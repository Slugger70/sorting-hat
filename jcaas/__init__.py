from galaxy.jobs import JobDestination
#from galaxy.jobs.mapper import JobMappingException

import backoff
import copy
import json
import logging
import math
import os
import requests
import subprocess
import time
import yaml

log = logging.getLogger(__name__)

# Maximum resources
CONDOR_MAX_CORES = 40
CONDOR_MAX_MEM = 250 - 2
SGE_MAX_CORES = 24
SGE_MAX_MEM = 256 - 2

# The default / base specification for the different environments.
SPECIFICATION_PATH = os.path.join(os.path.dirname(os.path.realpath(__file__)), os.pardir, 'config', 'destination_specifications.yaml')
with open(SPECIFICATION_PATH, 'r') as handle:
    SPECIFICATIONS = yaml.load(handle)

TOOL_DESTINATION_PATH = os.path.join(os.path.dirname(os.path.realpath(__file__)), os.pardir, 'config', 'tool_destinations.yaml')
with open(TOOL_DESTINATION_PATH, 'r') as handle:
    TOOL_DESTINATIONS = yaml.load(handle)

TRAINING_MACHINES = {}
STALE_CONDOR_HOST_INTERVAL = 60  # seconds


def get_tool_id(tool_id):
    """
    Convert ``toolshed.g2.bx.psu.edu/repos/devteam/column_maker/Add_a_column1/1.1.0``
    to ``Add_a_column``

    :param str tool_id: a tool id, can be the short kind (e.g. upload1) or the long kind with the full TS path.

    :returns: a short tool ID.
    :rtype: str
    """
    if tool_id.count('/') == 0:
        # E.g. upload1, etc.
        return tool_id

    # what about odd ones.
    if tool_id.count('/') == 5:
        (server, _, owner, repo, name, version) = tool_id.split('/')
        return name

    # No idea what this is.
    log.warning("Strange tool ID (%s), runner was not sure how to handle it.\n", tool_id)
    return tool_id


def name_it(tool_spec):
    if 'cores' in tool_spec:
        name = '%scores_%sG' % (tool_spec.get('cores', 1), tool_spec.get('mem', 4))
    elif len(tool_spec.keys()) == 0 or (len(tool_spec.keys()) == 1 and 'runner' in tool_spec):
        name = '%s_default' % tool_spec.get('runner', 'sge')
    else:
        name = '%sG_memory' % tool_spec.get('mem', 4)

    if tool_spec.get('tmp', None) == 'large':
        name += '_large'

    if 'name' in tool_spec:
        name += '_' + tool_spec['name']

    return name


def build_spec(tool_spec):
    destination = tool_spec.get('runner', 'sge')

    env = dict(SPECIFICATIONS.get(destination, {'env': {}})['env'])
    params = dict(SPECIFICATIONS.get(destination, {'params': {}})['params'])
    # A dictionary that stores the "raw" details that went into the template.
    raw_allocation_details = {}

    # We define the default memory and cores for all jobs. This is
    # semi-internal, and may not be properly propagated to the end tool
    tool_memory = tool_spec.get('mem', 4)
    tool_cores = tool_spec.get('cores', 1)
    # We apply some constraints to these values, to ensure that we do not
    # produce unschedulable jobs, requesting more ram/cpu than is available in a
    # given location. Currently we clamp those values rather than intelligently
    # re-scheduling to a different location due to TaaS constraints.
    if destination == 'sge':
        tool_memory = min(tool_memory, SGE_MAX_MEM)
        tool_cores = min(tool_cores, SGE_MAX_CORES)
    elif 'condor' in destination:
        tool_memory = min(tool_memory, CONDOR_MAX_MEM)
        tool_cores = min(tool_cores, CONDOR_MAX_CORES)

    kwargs = {
        # Higher numbers are lower priority, like `nice`.
        'PRIORITY': tool_spec.get('priority', 128),
        'MEMORY': str(tool_memory) + 'G',
        'PARALLELISATION': "",
        'NATIVE_SPEC_EXTRA': "",
    }
    # Allow more human-friendly specification
    if 'nativeSpecification' in params:
        params['nativeSpecification'] = params['nativeSpecification'].replace('\n', ' ').strip()

    # We have some destination specific kwargs. `nativeSpecExtra` and `tmp` are only defined for SGE
    if destination == 'sge':
        if 'cores' in tool_spec:
            kwargs['PARALLELISATION'] = '-pe "pe*" %s' % tool_cores
            # memory is defined per-core, and the input number is in gigabytes.
            real_memory = int(1024 * tool_memory / tool_spec['cores'])
            # Supply to kwargs with M for megabyte.
            kwargs['MEMORY'] = '%sM' % real_memory
            raw_allocation_details['mem'] = tool_memory
            raw_allocation_details['cpu'] = tool_cores

        if 'nativeSpecExtra' in tool_spec:
            kwargs['NATIVE_SPEC_EXTRA'] = tool_spec['nativeSpecExtra']

        # Large TMP dir
        if tool_spec.get('tmp', None) == 'large':
            kwargs['NATIVE_SPEC_EXTRA'] += '-l has_largetmp=1'

        # Environment variables, SGE specific.
        if 'env' in tool_spec and '_JAVA_OPTIONS' in tool_spec['env']:
            params['nativeSpecification'] = params['nativeSpecification'].replace('-v _JAVA_OPTIONS', '')
    elif 'condor' in destination:
        if 'cores' in tool_spec:
            kwargs['PARALLELISATION'] = tool_cores
            raw_allocation_details['cpu'] = tool_cores
        else:
            pass
            # del params['request_cpus']

        if 'mem' in tool_spec:
            raw_allocation_details['mem'] = tool_memory

        if 'requirements' in tool_spec:
            params['requirements'] = tool_spec['requirements']

        if 'rank' in tool_spec:
            params['rank'] = tool_spec['rank']

    # Update env and params from kwargs.
    env.update(tool_spec.get('env', {}))
    env = {k: str(v).format(**kwargs) for (k, v) in env.items()}
    params.update(tool_spec.get('params', {}))
    params = {k: str(v).format(**kwargs) for (k, v) in params.items()}

    if destination == 'sge':
        runner = 'drmaa'
    elif 'condor' in destination:
        runner = 'condor'
    else:
        runner = 'local'

    env = [dict(name=k, value=v) for (k, v) in env.items()]
    return env, params, runner, raw_allocation_details


def drmaa_is_available():
    try:
        os.stat('/usr/local/galaxy/temporarily-disable-drmaa')
        return False
    except OSError:
        return True


def condor_is_available():
    try:
        os.stat('/usr/local/galaxy/temporarily-disable-condor')
        return False
    except OSError:
        pass

    try:
        executors = subprocess.check_output(['condor_status'])
        # No executors, assume offline.
        if len(executors.strip()) == 0:
            return False

        return True
    except subprocess.CalledProcessError:
        return False
    except FileNotFoundError:
        # No condor binary
        return False


def get_training_machines(group='training'):
    # IF more than 60 seconds out of date, refresh.
    global TRAINING_MACHINES

    # Define the group if it doesn't exist.
    if group not in TRAINING_MACHINES:
        TRAINING_MACHINES[group] = {
            'updated': 0,
            'machines': [],
        }

    if time.time() - TRAINING_MACHINES[group]['updated'] > STALE_CONDOR_HOST_INTERVAL:
        # Fetch a list of machines
        try:
            machine_list = subprocess.check_output(['condor_status', '-long', '-attributes', 'Machine']).decode('utf8')
        except subprocess.CalledProcessError:
            machine_list = ''
        except FileNotFoundError:
            machine_list = ''

        # Strip them
        TRAINING_MACHINES[group]['machines'] = [
            x[len("Machine = '"):-1]
            for x in machine_list.strip().split('\n\n')
            if '-' + group + '-' in x
        ]
        # And record that this has been updated recently.
        TRAINING_MACHINES[group]['updated'] = time.time()
    return TRAINING_MACHINES[group]['machines']


def avoid_machines(permissible=None):
    """
    Obtain a list of the special training machines in the form that can be used
    in a rank/requirement expression.

    :param permissible: A list of training groups that are permissible to the user and shouldn't be included in the expression
    :type permissible: list(str) or None

    """
    if permissible is None:
        permissible = []
    machines = set(get_training_machines())
    # List of those to remove.
    to_remove = set()
    # Loop across permissible machines in order to remove them from the machine dict.
    for allowed in permissible:
        for m in machines:
            if allowed in m:
                to_remove = to_remove.union(set([m]))
    # Now we update machine list with removals.
    machines = machines.difference(to_remove)
    # If we want to NOT use the machines, construct a list with `!=`
    data = ['(machine != "%s")' % m for m in sorted(machines)]
    if len(data):
        return '( ' + ' && '.join(data) + ' )'
    return ''


def prefer_machines(training_identifiers, machine_group='training'):
    """
    Obtain a list of the specially tagged machines in the form that can be used
    in a rank/requirement expression.

    :param training_identifiers: A list of training groups that are permissible to the user and shouldn't be included in the expression
    :type training_identifiers: list(str) or None
    """
    if training_identifiers is None:
        training_identifiers = []

    machines = set(get_training_machines(group=machine_group))
    allowed = set()
    for identifier in training_identifiers:
        for m in machines:
            if identifier in m:
                allowed = allowed.union(set([m]))

    # If we want to use the machines, construct a list with `==`
    data = ['(machine == "%s")' % m for m in sorted(allowed)]
    if len(data):
        return '( ' + ' || '.join(data) + ' )'
    return ''


def reroute_to_dedicated(tool_spec, user_roles):
    """
    Re-route users to correct destinations. Some users will be part of a role
    with dedicated training resources.
    """
    # Collect their possible training roles identifiers.
    training_roles = [role[len('training-'):] for role in user_roles if role.startswith('training-')]

    # No changes to specification.
    if len(training_roles) == 0:
        # However if it is running on condor, make sure that it doesn't run on the training machines.
        if 'runner' in tool_spec and tool_spec['runner'] == 'condor':
            # Require that the jobs do not run on these dedicated training machines.
            return {'requirement': avoid_machines()}
        # If it isn't running on condor, no changes.
        return {}

    # Otherwise, the user does have one or more training roles.
    # So we must construct a requirement / ranking expression.
    return {
        # We require that it does not run on machines that the user is not in the role for.
        'requirements': avoid_machines(permissible=training_roles),
        # We then rank based on what they *do* have the roles for
        'rank': prefer_machines(training_roles),
        'runner': 'condor',
    }


def _finalize_tool_spec(tool_id, user_roles, memory_scale=1.0):
    # Find the 'short' tool ID which is what is used in the .yaml file.
    tool = get_tool_id(tool_id)
    # Pull the tool specification (i.e. job destination configuration for this tool)
    tool_spec = copy.deepcopy(TOOL_DESTINATIONS.get(tool, {}))
    # Update the tool specification with any training resources that are available
    tool_spec.update(reroute_to_dedicated(tool_spec, user_roles))

    tool_spec['mem'] = tool_spec.get('mem', 4) * memory_scale

    # Only two tools are truly special.
    if tool_id == 'upload1':
        tool_spec = {
            'mem': 0.3,
            'runner': 'condor',
            'requirements': prefer_machines(['upload'], machine_group='upload'),
            'env': {
                'TEMP': '/data/1/galaxy_db/tmp/'
            }
        }
    elif tool_id == '__SET_METADATA__':
        tool_spec = {
            'mem': 0.3,
            'runner': 'condor',
            'requirements': prefer_machines(['metadata'], machine_group='metadata')
        }
    return tool_spec


def convert_condor_to_sge(tool_spec):
    # Send this to SGE
    tool_spec['runner'] = 'sge'
    # SGE does not support partials
    tool_spec['mem'] = int(math.ceil(tool_spec['mem']))
    return tool_spec


def convert_sge_to_condor(tool_spec):
    tool_spec['runner'] = 'condor'
    return tool_spec


def handle_downed_runners(tool_spec):
    # In the event that it was going to condor and condor is unavailable, re-schedule to sge
    avail_condor = condor_is_available()
    avail_drmaa = drmaa_is_available()

    if not avail_condor and not avail_drmaa:
        raise Exception("Both clusters are currently down")

    if tool_spec.get('runner', 'local') == 'condor':
        if avail_drmaa and not avail_condor:
            tool_spec = convert_condor_to_sge(tool_spec)

    elif tool_spec.get('runner', 'local') == 'sge':
        if avail_condor and not avail_drmaa:
            tool_spec = convert_condor_to_sge(tool_spec)

    return tool_spec


def _gateway(tool_id, user_roles, user_email, memory_scale=1.0):
    tool_spec = handle_downed_runners(_finalize_tool_spec(tool_id, user_roles, memory_scale=memory_scale))

    # Send special users to condor temporarily.
    if 'gx-admin-force-jobs-to-condor' in user_roles:
        tool_spec = convert_sge_to_condor(tool_spec)
    if 'gx-admin-force-jobs-to-drmaa' in user_roles:
        tool_spec = convert_condor_to_sge(tool_spec)

    if tool_id == 'echo_main_env':
        if user_email != 'hxr@informatik.uni-freiburg.de':
            raise Exception("Unauthorized")
        else:
            tool_spec = convert_sge_to_condor(tool_spec)

    # Now build the full spec
    env, params, runner, _ = build_spec(tool_spec)

    return env, params, runner, tool_spec


@backoff.on_exception(backoff.fibo,
                      # Parent class of all requests exceptions, should catch
                      # everything.
                      requests.exceptions.RequestException,
                      # https://aws.amazon.com/blogs/architecture/exponential-backoff-and-jitter/
                      jitter=backoff.full_jitter,
                      max_tries=8)
def _gateway2(tool_id, user_roles, user_email, memory_scale=1.0):
    payload = {
        'tool_id': tool_id,
        'user_roles': user_roles,
        'email': user_email,
    }
    r = requests.post('http://127.0.0.1:8090', data=json.dumps(payload), timeout=1, headers={'Content-Type': 'application/json'})
    data = r.json()
    return data['env'], data['params'], data['runner'], data['spec']


def gateway(tool_id, user, memory_scale=1.0):
    # And run it.
    if user:
        user_roles = [role.name for role in user.all_roles() if not role.deleted]
        email = user.email
    else:
        user_roles = []
        email = ''

    try:
        env, params, runner, spec = _gateway2(tool_id, user_roles, email, memory_scale=memory_scale)
    except requests.exceptions.RequestException:
        # We really failed, so fall back to old algo.
        env, params, runner, spec = _gateway(tool_id, user_roles, email, memory_scale=memory_scale)

    name = name_it(spec)
    return JobDestination(
        id=name,
        runner=runner,
        params=params,
        env=env,
        resubmit=[{
            'condition': 'any_failure',
            'destination': 'resubmit_gateway',
        }]
    )


def resubmit_gateway(tool_id, user):
    """Gateway to handle jobs which have been resubmitted once.

    We don't want to try re-running them forever so the ONLY DIFFERENCE in
    these methods is that this one doesn't include a 'resubmission'
    specification in the returned JobDestination
    """

    job_destination = gateway(tool_id, user, memory_scale=1.5)
    job_destination['resubmit'] = []
    job_destination['id'] = job_destination['id'] + '_resubmit'
    return job_destination


def toXml(env, params, runner, spec):
    name = name_it(spec)

    print('        <destination id="%s" runner="%s">' % (name, runner))
    for (k, v) in params.items():
        print('            <param id="%s">%s</param>' % (k, v))
    for k in env:
        print('            <env id="%s">%s</env>' % (k['name'], k['value']))
    print('        </destination>')
    print("")


if __name__ == '__main__':
    seen_destinations = []
    for tool in TOOL_DESTINATIONS:
        if TOOL_DESTINATIONS[tool] not in seen_destinations:
            seen_destinations.append(TOOL_DESTINATIONS[tool])

    for spec in seen_destinations:
        (env, params, runner, _) = build_spec(spec)
        toXml(env, params, runner, spec)

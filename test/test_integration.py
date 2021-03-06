import argparse
import psutil
import signal
import subprocess
import threading
import time
import uuid
import tempfile
import shutil
import pytest
import os

from rebus.agent import Agent, AgentRegistry
from rebus.bus import BusRegistry, DEFAULT_DOMAIN
import rebus.agents
import rebus.buses

rebus.agents.import_all()
rebus.buses.import_all()


# This file implements integration testing - injects a file to the bus, checks
# that it has properly been received by agents & storage, that no agent has
# crashed.

# Warning: py.test hides exceptions that happen during the test; run REbus
# without py.test when tests misbehave

# TODO tests (cf bta/test/test_miners): all agents are registered / instantiate
# without crashing

# TODO add argument parsing tests - check that help is displayed


@pytest.fixture(scope='function', params=['diskstorage', 'ramstorage'])
def storage(request):
    """
    Returns a string that describes the storage type, and a list containing
    arguments for the storage backend.
    Perform setup & teardown for storage.
    """
    # py.test-provided fixture "tmpdir" does not guarantee an empty temp
    # directory, which get re-used when test is run again - rolling our own...
    args = []
    if request.param == 'diskstorage':
        tmpdir = tempfile.mkdtemp('rebus-test-%s' % request.param)
        args = ['diskstorage', '--path', tmpdir]

        def fin():
            shutil.rmtree(tmpdir)
        request.addfinalizer(fin)

    return (request.param, args)


@pytest.fixture(scope='function', params=['localbus', 'dbus', 'rabbit'])
def bus(request, storage):
    """
    Returns fixture parameters and a function that returns a bus instance.
    """
    storagetype, storageparams = storage
    if request.param == 'dbus':
        check_master_not_running()
        # launch rebus master
        process = subprocess.Popen(['rebus_master', 'dbus'] + storageparams,
                                   stderr=subprocess.PIPE)
        # wait for master bus to be ready - TODO look into & fix race
        time.sleep(0.5)
        output = ""
        # output = process.stderr.read(1)

        def fin():
            process.send_signal(signal.SIGINT)
            process.wait()
            assert process.returncode == 0, output + process.stderr.read()

        request.addfinalizer(fin)

        def return_bus():
            busclass = rebus.bus.BusRegistry.get(request.param)
            bus_parser = argparse.ArgumentParser()
            busclass.add_arguments(bus_parser)
            bus_options = bus_parser.parse_args([])
            return busclass(bus_options)
    elif request.param == 'rabbit':
        check_master_not_running()
        # launch rebus master
        args = "rebus_master -v rabbit".split(' ')
        process = subprocess.Popen(args + storageparams,
                                   stderr=subprocess.PIPE)
        # wait for master bus to be ready - TODO look into & fix race

        # TODO check queues are empty or empty them
        # until then: run the following commands to empty queues
        # rabbitmqctl stop_app; rabbitmqctl reset; rabbitmqctl start_app
        time.sleep(1)

        def fin():
            os.kill(process.pid, signal.SIGINT)
            process.wait()
            assert process.returncode == 0, process.stderr.read()

        request.addfinalizer(fin)

        def return_bus():
            busclass = rebus.bus.BusRegistry.get(request.param)
            bus_parser = argparse.ArgumentParser()
            busclass.add_arguments(bus_parser)
            bus_options = bus_parser.parse_args([])
            return busclass(bus_options)
    elif request.param == 'localbus':
        # always return the same bus instance
        if storagetype == 'diskstorage':
            pytest.skip("diskstorage is not supported by localbus")
        bus_options = argparse.Namespace()
        instance = BusRegistry.get(request.param)(bus_options)

        def return_bus():
            return instance
    return (request.param, storagetype, return_bus)


def check_master_not_running():
    # 'rebus_master' is too long - gets truncated
    running = any(['rebus_master' in p.name() for p in psutil.process_iter()])
    assert running is False, "rebus_master_dbus is already running"


def parse_arguments(agent_class, args):
    """
    Returns a namespace containing parsed arguments for the requested agent.

    :param args: list of arguments
    """
    parser = argparse.ArgumentParser()
    agent_class.add_agent_arguments(parser)
    options, _ = parser.parse_known_args(args)
    return options


@pytest.fixture(scope='function')
def agent_test(bus):
    """
    Returns an instance of a test agent, registered to the bus and running in
    another thread.
    """
    bustype, storagetype, returnbus = bus

    @Agent.register
    class TestAgent(Agent):
        _name_ = "testagent_%s_%s" % (bustype, storagetype)
        _desc_ = "Accepts any input. Records received selectors, descriptors"

        received_selectors = []
        processed_descriptors = []

        def selector_filter(self, selector):
            self.received_selectors.append(selector)
            return True

        def process(self, desc, sender_id):
            self.processed_descriptors.append((desc, sender_id))

    namespace = parse_arguments(TestAgent, [])
    agent = TestAgent(bus=returnbus(), domain='default', options=namespace)
    return agent


@pytest.fixture(scope='function')
def agent_inject(bus, request):
    bustype, storagetype, returnbus = bus
    if bustype == 'localbus':
        bus_instance = returnbus()
        agent_class = AgentRegistry.get('inject')
        namespace = parse_arguments(agent_class, ['/bin/ls'])
        agent = agent_class(bus=bus_instance, domain='default',
                            options=namespace)
        return agent
    elif bustype in ('rabbit', 'dbus'):
        # Running two DBUS agents in the same process does not work yet -
        # dbus signal handler related problem
        returncode = subprocess.call(('rebus_agent', '--bus', bustype,
                                      'inject', '/bin/ls'))
        assert returncode == 0
        return


@pytest.fixture(scope='function')
def agent_set(bus):
    """
    Run predefined sets of agents on the bus. Check that they did not crash at
    the end.
    """
    # TODO
    pass


@pytest.mark.parametrize('busname', ['dbus', 'rabbit'])
def test_master(busname):
    """
    Run, then stop rebus_master_dbus
    """
    check_master_not_running()

    process = subprocess.Popen(['rebus_master', busname],
                               stderr=subprocess.PIPE, bufsize=0)
    # wait for master bus to be ready
    # TODO look into race condition. Another SIGINT handler?
    time.sleep(2)
    output = process.stderr.read(1)
    process.send_signal(signal.SIGINT)
    process.wait()
    assert process.returncode == 0, output + process.stderr.read()


def test_inject(agent_set, agent_test, agent_inject):
    """
    * Inject a file to the bus
    * Check that is can be fetched from the bus interface
    * Check that it has been received by the test agent
    * Make sure no agent has thrown any exception
    """
    bus_instance = agent_test.bus
    t = threading.Thread(target=bus_instance.run_agents)
    t.daemon = True
    t.start()

    # TODO cleanly make sure all agents from agentset have finished processing
    time.sleep(1)

    injected_value = open('/bin/ls', 'rb').read()
    # Fetch using the bus interface, check value
    # Find by selector regexp
    selectors = bus_instance.find(agent_test.id, DEFAULT_DOMAIN,
                                  '/binary/elf', 10)
    assert len(selectors) > 0
    # Get descriptor
    descriptor = bus_instance.get(agent_test.id, DEFAULT_DOMAIN,
                                  selectors[0])
    assert descriptor.value == injected_value
    assert descriptor.domain == 'default'
    assert descriptor.agent == 'inject'
    assert descriptor.label == 'ls'
    assert descriptor.precursors == []
    assert descriptor.selector.startswith('/binary/elf/%')
    assert uuid.UUID(descriptor.uuid) is not None
    assert descriptor.version == 0
    # Get descriptor by version
    descriptor_version = bus_instance.get(agent_test.id, DEFAULT_DOMAIN,
                                          selectors[0].split('%')[0]+'~-1')

    # force fetching descriptor value
    assert descriptor_version.value == descriptor.value
    assert descriptor_version == descriptor

    assert descriptor == descriptor_version

    # Find by value regexp
    descriptors_byvalue = bus_instance.find_by_value(agent_test.id,
                                                     DEFAULT_DOMAIN, '/binary',
                                                     injected_value[0:4])
    # Check UUID exists & can be found
    assert descriptors_byvalue[0].value == descriptor.value
    uuids = bus_instance.list_uuids("testid", DEFAULT_DOMAIN)
    assert descriptor.uuid in uuids

    descriptors_uuid = bus_instance.find_by_uuid(agent_test.id, DEFAULT_DOMAIN,
                                                 descriptor.uuid)
    # Find by selector
    bysel = bus_instance.find_by_selector(agent_test.id, DEFAULT_DOMAIN,
                                          '/binary')
    assert len(bysel) == 1
    assert bysel[0].value == descriptor.value

    # force fetching descriptor value
    assert descriptors_uuid[0].value == descriptor.value
    assert descriptors_uuid[0] == descriptor

    # Check that it has been received by TestAgent
    received = agent_test.received_selectors
    processed = agent_test.processed_descriptors
    assert received == selectors
    # force fetching descriptor value
    assert processed[0][0].value == descriptor.value
    assert processed[0][0] == descriptor

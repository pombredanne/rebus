import threading
from collections import Counter, defaultdict, namedtuple
from rebus.bus import Bus, DEFAULT_DOMAIN
from rebus.storage_backends.ramstorage import RAMStorage
import logging

log = logging.getLogger("rebus.localbus")
agent_desc = namedtuple("agent_desc", ("agent_id", "domain", "callback"))


@Bus.register
class LocalBus(Bus):
    _name_ = "localbus"

    def __init__(self, busaddr=None):
        Bus.__init__(self)
        self.callbacks = []
        self.locks = defaultdict(set)
        self.agent_count = 0
        self.store = RAMStorage()  # TODO add support for DiskStorage ?
        # TODO save internal state at bus exit (only useful with DiskStorage)
        #: maps agentid (ex. inject-12) to agentdesc
        self.agents = {}
        self.threads = []

    def join(self, agent, agent_domain=DEFAULT_DOMAIN, callback=None):
        agid = "%s-%i" % (agent.name, self.agent_count)
        self.agent_count += 1
        if callback:
            self.callbacks.append((agid, callback))
        self.agents[agid] = agent_desc(agid, agent_domain, callback)
        return agid

    def lock(self, agent_id, lockid, desc_domain, selector):
        key = (lockid, desc_domain, selector)
        log.info("LOCK:%s %s => %r %s:%s ", lockid, agent_id, key in
                 self.locks[desc_domain], desc_domain, selector)
        if key in self.locks[desc_domain]:
            return False
        self.locks[desc_domain].add(key)
        return True

    def push(self, agent_id, descriptor):
        desc_domain = descriptor.domain
        selector = descriptor.selector
        if self.store.add(descriptor):
            log.info("PUSH: %s => %s:%s", agent_id, desc_domain, selector)
            for agid, cb in self.callbacks:
                try:
                    log.debug("Calling %s callback", agid)
                    cb(agent_id.id, desc_domain, selector)
                except Exception, e:
                    log.error("ERROR agent [%s]: %s", agid, e)
        else:
            log.info("PUSH: %s already seen => %s:%s", agent_id, desc_domain,
                     selector)

    def get(self, agent_id, desc_domain, selector):
        log.info("GET: %s %s:%s", agent_id, desc_domain, selector)
        return self.store.get_descriptor(desc_domain, selector,
                                         serialized=False)

    def get_value(self, agent_id, desc_domain, selector):
        log.info("GET: %s %s:%s", agent_id, desc_domain, selector)
        return self.store.get_value(desc_domain, selector)

    def list_uuids(self, agent_id, desc_domain):
        log.debug("LISTUUIDS: %s %s", agent_id, desc_domain)
        return self.store.list_uuids(desc_domain)

    def find(self, agent_id, desc_domain, selector_regex, limit):
        log.debug("FIND: %s %s:%s (%d)", agent_id, desc_domain, selector_regex,
                  limit)
        return self.store.find(desc_domain, selector_regex, limit)

    def find_by_uuid(self, agent_id, desc_domain, uuid):
        log.debug("FINDBYUUID: %s %s:%s", agent_id, desc_domain, uuid)
        return self.store.find_by_uuid(desc_domain, uuid, serialized=False)

    def mark_processed(self, desc_domain, selector, agent_id, config_txt):
        log.debug("MARK_PROCESSED: %s:%s %s %s", desc_domain, selector,
                  agent_id, config_txt)
        self.store.mark_processed(desc_domain, selector, agent_id, config_txt)

    def list_agents(self, agent_id):
        return dict(Counter(i.rsplit('-', 1)[0] for i in self.agents.keys()))

    def processed_stats(self, agent_id, desc_domain):
        log.debug("PROCESSED_STATS: %s %s", agent_id, desc_domain)
        return self.store.processed_stats(desc_domain)

    def get_children(self, agent_id, desc_domain, selector, recurse=True):
        log.info("GET_CHILDREN: %s %s:%s", agent_id, desc_domain, selector)
        return list(self.store.get_children(desc_domain, selector,
                                            recurse, serialized=False))

    def store_internal_state(self, agent_id, state):
        log.debug("STORE_INTSTATE: %s", agent_id)
        if self.store.STORES_INTSTATE:
            self.store.store_state(str(agent_id), str(state))

    def load_internal_state(self, agent_id):
        log.debug("LOAD_INTSTATE: %s", agent_id)
        if self.store.STORES_INTSTATE:
            return self.store.load_state(str(agent_id))
        return ""

    def run_agent(self, agent, args):
        t = threading.Thread(target=agent.run, args=args)
        t.daemon = True
        t.start()
        self.threads.append(t)

    def agentloop(self, agent):
        pass

    def busloop(self):
        for t in self.threads:
            t.join()

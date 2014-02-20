"""
Created on Jan 22, 2014

@author: Vadim Markovtsev <v.markovtsev@samsung.com>
"""


from fysom import Fysom
import json
import logging
from twisted.internet import reactor, threads
from twisted.internet.protocol import ReconnectingClientFactory

from daemon import daemonize
import network_common


def onFSMStateChanged(e):
        """
        Logs the current state transition.
        """
        logging.debug("slave state: %s, %s -> %s", e.event, e.src, e.dst)


class VelesProtocol(network_common.StringLineReceiver):
    """A communication controller from client to server.

    Attributes:
        FSM_DESCRIPTION     The definition of the Finite State Machine of the
                            protocol.
    """

    FSM_DESCRIPTION = {
        'initial': 'INIT',
        'events': [
            {'name': 'disconnect', 'src': '*', 'dst': 'ERROR'},
            {'name': 'request_id', 'src': ['INIT', 'WAIT'], 'dst': 'WAIT'},
            {'name': 'request_job', 'src': ['WAIT', 'GETTING_JOB'],
                                    'dst': 'GETTING_JOB'},
            {'name': 'obtain_job', 'src': 'GETTING_JOB', 'dst': 'BUSY'},
            {'name': 'refuse_job', 'src': 'GETTING_JOB', 'dst': 'GETTING_JOB'},
            {'name': 'wait_update_notification', 'src': 'BUSY', 'dst': 'WAIT'},
        ],
        'callbacks': {
            'onchangestate': onFSMStateChanged
        }
    }

    def __init__(self, addr, factory):
        """
        Initializes the protocol.

        Parameters:
            addr        The address of the server (reported by Twisted).
            factory     The factory which produced this protocol.
        """
        super(VelesProtocol, self).__init__()
        self.addr = addr
        self.factory = factory
        self.id = 'None'
        if not factory.state:
            factory.state = Fysom(VelesProtocol.FSM_DESCRIPTION)
        self.state = factory.state

    def connectionMade(self):
        if self.state.current == "INIT" or self.state.current == "WAIT":
            self.request_id()
            return
        if self.state.current == "GETTING_JOB":
            self.send_id()
            self.request_job()
            return
        if self.state.current == "BUSY":
            self.send_id()
            self.state.obtain_job()
            return

    def connectionLost(self, reason):
        logging.debug("Connection was lost.")

    def lineReceived(self, line):
        logging.debug("lineReceived %s:  %s", self.id, line)
        msg = json.loads(line.decode("utf-8"))
        if not isinstance(msg, dict):
            logging.error("Could not parse the received line, dropping it.")
            return
        error = msg.get("error")
        if error:
            self.disconnect("Server returned error: '%s'.", error)
            return
        if self.state.current == "WAIT":
            update = msg.get("update")
            if not update:
                cid = msg.get("id")
                if not cid:
                    logging.error("No id is present.")
                    self.request_id()
                    return
                self.factory.id = cid
            self.request_job()
            return
        if self.state.current == "GETTING_JOB":
            job = msg.get("job")
            if not job:
                self.disconnect("No job is present.")
                return
            if job == "refuse":
                logging.info("Job was refused.")
                self.state.refuse_job()
                self.factory.host.stop()
                return
            if job != "offer":
                self.disconnect("Unknown job value %s.", job)
                return
            self.size = msg.get("size")
            if self.size == None:
                self.disconnect("Job size was not specified.")
                return
            self.job = bytearray(self.size)
            self.job_pos = 0
            self.setRawMode()
            return
        self.disconnect("Invalid state %s.", self.state.current)

    def rawDataReceived(self, data):
        if self.state.current == 'GETTING_JOB':
            self.job[self.job_pos:self.job_pos + len(data)] = data
            self.job_pos += len(data)
            if self.job_pos == self.size:
                self.setLineMode()
                job = threads.deferToThreadPool(reactor,
                                    self.factory.host.workflow.thread_pool(),
                                    self.factory.host.workflow.do_job,
                                    self.job)
                job.addCallback(self.jobFinished)
                self.state.obtain_job()
            if len(self.job) > self.size:
                self.disconnect("Received job size %d exceeded the expected "
                                "length (%d)", len(self.job), self.size)
            return
        self.disconnect("Invalid state %s.", self.state.current)

    def jobFinished(self, update):
        if self.state.current == "BUSY":
            self.sendLine({'cmd': 'update', 'size': len(update)})
            self.transport.write(update)
            self.state.wait_update_notification()
            return
        logging.error("Invalid state %s.", self.state.current)

    def sendLine(self, line):
        super(VelesProtocol, self).sendLine(json.dumps(line))

    def send_id(self):
        self.sendLine({'id': self.factory.id,
                       'power':
                       self.factory.host.workflow.get_computing_power(),
                       'checksum': self.factory.host.workflow.checksum()})

    def request_id(self):
        self.sendLine({'power':
                       self.factory.host.workflow.get_computing_power(),
                       'checksum': self.factory.host.workflow.checksum()})
        self.state.request_id()

    def request_job(self):
        self.sendLine({'cmd': 'job'})
        self.state.request_job()

    def disconnect(self, msg, *args, **kwargs):
        logging.error(msg, *args, **kwargs)
        self.state.disconnect()
        self.transport.loseConnection()


class VelesProtocolFactory(ReconnectingClientFactory):
    def __init__(self, host):
        super(VelesProtocolFactory, self).__init__()
        self.host = host
        self.id = None
        self.state = None

    def startedConnecting(self, connector):
        logging.info('Connecting...')

    def buildProtocol(self, addr):
        return VelesProtocol(addr, self)

    def clientConnectionLost(self, connector, reason):
        if self.state.current != 'ERROR':
            logging.warning("Disconnected, trying to reconnect...")
            connector.connect()
        else:
            logging.info("Disconnected.")

    def clientConnectionFailed(self, connector, reason):
        logging.warn('Connection failed. Reason: %s', reason)


class Client(network_common.NetworkConfigurable):
    """
    UDT/TCP client operating on a single socket.
    """

    def __init__(self, configuration, workflow):
        super(Client, self).__init__(configuration)
        self.workflow = workflow
        self.factory = VelesProtocolFactory(self)
        reactor.connectTCP(self.address, self.port, self.factory)

    @daemonize
    def run(self):
        try:
            reactor.run()
        except:
            logging.exception("Failed to run the reactor")

    def stop(self):
        try:
            if reactor.running:
                reactor.stop()
        except:
            logging.exception("Failed to stop the reactor")

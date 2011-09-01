import re
import json
import redis

from twisted.python import log
from twisted.internet.protocol import Protocol, ReconnectingClientFactory
from twisted.internet.task import LoopingCall

import binascii
from smpp.pdu import unpack_pdu
from smpp.pdu_builder import (BindTransceiver,
                                DeliverSMResp,
                                SubmitSM,
                                SubmitMulti,
                                EnquireLink,
                                EnquireLinkResp,
                                QuerySM
                                )
from smpp.pdu_inspector import (MultipartMessage,
                                detect_multipart,
                                multipart_key
                                )

from vumi.utils import get_deploy_int

# for testing with trial
#import sys
#log.startLogging(sys.stdout)

# TODO this will move to pdu_inspector in python-smpp
ESME_command_status_map = {
    "ESME_ROK"              : "No Error",
    "ESME_RINVMSGLEN"       : "Message Length is invalid",
    "ESME_RINVCMDLEN"       : "Command Length is invalid",
    "ESME_RINVCMDID"        : "Invalid Command ID",
    "ESME_RINVBNDSTS"       : "Incorrect BIND Status for given command",
    "ESME_RALYBND"          : "ESME Already in Bound State",
    "ESME_RINVPRTFLG"       : "Invalid Priority Flag",
    "ESME_RINVREGDLVFLG"    : "Invalid Registered Delivery Flag",
    "ESME_RSYSERR"          : "System Error",
    "ESME_RINVSRCADR"       : "Invalid Source Address",
    "ESME_RINVDSTADR"       : "Invalid Dest Addr",
    "ESME_RINVMSGID"        : "Message ID is invalid",
    "ESME_RBINDFAIL"        : "Bind Failed",
    "ESME_RINVPASWD"        : "Invalid Password",
    "ESME_RINVSYSID"        : "Invalid System ID",
    "ESME_RCANCELFAIL"      : "Cancel SM Failed",
    "ESME_RREPLACEFAIL"     : "Replace SM Failed",
    "ESME_RMSGQFUL"         : "Message Queue Full",
    "ESME_RINVSERTYP"       : "Invalid Service Type",
    "ESME_RINVNUMDESTS"     : "Invalid number of destinations",
    "ESME_RINVDLNAME"       : "Invalid Distribution List name",
    "ESME_RINVDESTFLAG"     : "Destination flag is invalid (submit_multi)",
    "ESME_RINVSUBREP"       : "Invalid 'submit with replace' request (i.e. submit_sm with replace_if_present_flag set)",
    "ESME_RINVESMCLASS"     : "Invalid esm_class field data",
    "ESME_RCNTSUBDL"        : "Cannot Submit to Distribution List",
    "ESME_RSUBMITFAIL"      : "submit_sm or submit_multi failed",
    "ESME_RINVSRCTON"       : "Invalid Source address TON",
    "ESME_RINVSRCNPI"       : "Invalid Source address NPI",
    "ESME_RINVDSTTON"       : "Invalid Destination address TON",
    "ESME_RINVDSTNPI"       : "Invalid Destination address NPI",
    "ESME_RINVSYSTYP"       : "Invalid system_type field",
    "ESME_RINVREPFLAG"      : "Invalid replace_if_present flag",
    "ESME_RINVNUMMSGS"      : "Invalid number of messages",
    "ESME_RTHROTTLED"       : "Throttling error (ESME has exceeded allowed message limits)",
    "ESME_RINVSCHED"        : "Invalid Scheduled Delivery Time",
    "ESME_RINVEXPIRY"       : "Invalid message validity period (Expiry time)",
    "ESME_RINVDFTMSGID"     : "Predefined Message Invalid or Not Found",
    "ESME_RX_T_APPN"        : "ESME Receiver Temporary App Error Code",
    "ESME_RX_P_APPN"        : "ESME Receiver Permanent App Error Code",
    "ESME_RX_R_APPN"        : "ESME Receiver Reject Message Error Code",
    "ESME_RQUERYFAIL"       : "query_sm request failed",
    "ESME_RINVOPTPARSTREAM" : "Error in the optional part of the PDU Body.",
    "ESME_ROPTPARNOTALLWD"  : "Optional Parameter not allowed",
    "ESME_RINVPARLEN"       : "Invalid Parameter Length.",
    "ESME_RMISSINGOPTPARAM" : "Expected Optional Parameter missing",
    "ESME_RINVOPTPARAMVAL"  : "Invalid Optional Parameter Value",
    "ESME_RDELIVERYFAILURE" : "Delivery Failure (used for data_sm_resp)",
    "ESME_RUNKNOWNERR"      : "Unknown Error",
}


class EsmeTransceiver(Protocol):

    def __init__(self, seq, config, vumi_options):
        self.build_maps()
        self.name = 'Proto' + str(seq)
        log.msg('__init__', self.name)
        self.defaults = {}
        self.state = 'CLOSED'
        log.msg(self.name, 'STATE :', self.state)
        self.seq = seq
        self.config = config
        self.vumi_options = vumi_options
        self.inc = int(self.config['smpp_increment'])
        self.datastream = ''
        self.__connect_callback = None
        self.__submit_sm_resp_callback = None
        self.__delivery_report_callback = None
        self.__deliver_sm_callback = None
        self._send_failure_callback = None
        self.error_handlers = {
                "ok": self.dummy_ok,
                "mess_permfault": self.dummy_mess_permfault,
                "mess_tempfault": self.dummy_mess_tempfault,
                "conn_permfault": self.dummy_conn_permfault,
                "conn_tempfault": self.dummy_conn_tempfault,
                "conn_throttle": self.dummy_conn_throttle,
                }
        self.r_server = redis.Redis("localhost",
                db=get_deploy_int(self.vumi_options['vhost']))
        log.msg("Connected to Redis")
        self.r_prefix = "%s@%s:%s" % (
                self.config['system_id'],
                self.config['host'],
                self.config['port'])
        log.msg("r_prefix = %s" % self.r_prefix)

    def logmsg(selfm):
        print n

    # Dummy error handler functions, just log invocation
    def dummy_ok(self, *args, **kwargs):
            m = "%s.%s(*args=%s, **kwargs=%s)" % (
                __name__,
                "dummy_ok",
                args,
                kwargs)
            #log.msg(m)

    # Dummy error handler functions, just log invocation
    def dummy_mess_permfault(self, *args, **kwargs):
            m = "%s.%s(*args=%s, **kwargs=%s)" % (
                __name__,
                "dummy_mess_permfault",
                args,
                kwargs)
            log.msg(m)

    # Dummy error handler functions, just log invocation
    def dummy_mess_tempfault(self, *args, **kwargs):
            m = "%s.%s(*args=%s, **kwargs=%s)" % (
                __name__,
                "dummy_mess_tempfault",
                args,
                kwargs)
            log.msg(m)

    # Dummy error handler functions, just log invocation
    def dummy_conn_permfault(self, *args, **kwargs):
            m = "%s.%s(*args=%s, **kwargs=%s)" % (
                __name__,
                "dummy_conn_permfault",
                args,
                kwargs)
            log.msg(m)

    # Dummy error handler functions, just log invocation
    def dummy_conn_tempfault(self, *args, **kwargs):
            m = "%s.%s(*args=%s, **kwargs=%s)" % (
                __name__,
                "dummy_conn_tempfault",
                args,
                kwargs)
            log.msg(m)

    # Dummy error handler functions, just log invocation
    def dummy_conn_throttle(self, *args, **kwargs):
            m = "%s.%s(*args=%s, **kwargs=%s)" % (
                __name__,
                "dummy_conn_throttle",
                args,
                kwargs)
            log.msg(m)

    def build_maps(self):
        self.ESME_command_status_dispatch_map = {
            "ESME_ROK"              : self.dispatch_ok,
            "ESME_RINVMSGLEN"       : self.dispatch_mess_permfault,
            "ESME_RINVCMDLEN"       : self.dispatch_mess_permfault,
            "ESME_RINVCMDID"        : self.dispatch_mess_permfault,

            "ESME_RINVBNDSTS"       : self.dispatch_conn_tempfault,
            "ESME_RALYBND"          : self.dispatch_conn_tempfault,

            "ESME_RINVPRTFLG"       : self.dispatch_mess_permfault,
            "ESME_RINVREGDLVFLG"    : self.dispatch_mess_permfault,

            "ESME_RSYSERR"          : self.dispatch_conn_permfault,

            "ESME_RINVSRCADR"       : self.dispatch_mess_permfault,
            "ESME_RINVDSTADR"       : self.dispatch_mess_permfault,
            "ESME_RINVMSGID"        : self.dispatch_mess_permfault,

            "ESME_RBINDFAIL"        : self.dispatch_conn_permfault,
            "ESME_RINVPASWD"        : self.dispatch_conn_permfault,
            "ESME_RINVSYSID"        : self.dispatch_conn_permfault,

            "ESME_RCANCELFAIL"      : self.dispatch_mess_permfault,
            "ESME_RREPLACEFAIL"     : self.dispatch_mess_permfault,

            "ESME_RMSGQFUL"         : self.dispatch_conn_throttle,

            "ESME_RINVSERTYP"       : self.dispatch_conn_permfault,

            "ESME_RINVNUMDESTS"     : self.dispatch_mess_permfault,
            "ESME_RINVDLNAME"       : self.dispatch_mess_permfault,
            "ESME_RINVDESTFLAG"     : self.dispatch_mess_permfault,
            "ESME_RINVSUBREP"       : self.dispatch_mess_permfault,
            "ESME_RINVESMCLASS"     : self.dispatch_mess_permfault,
            "ESME_RCNTSUBDL"        : self.dispatch_mess_permfault,

            "ESME_RSUBMITFAIL"      : self.dispatch_mess_tempfault,

            "ESME_RINVSRCTON"       : self.dispatch_mess_permfault,
            "ESME_RINVSRCNPI"       : self.dispatch_mess_permfault,
            "ESME_RINVDSTTON"       : self.dispatch_mess_permfault,
            "ESME_RINVDSTNPI"       : self.dispatch_mess_permfault,

            "ESME_RINVSYSTYP"       : self.dispatch_conn_permfault,

            "ESME_RINVREPFLAG"      : self.dispatch_mess_permfault,

            "ESME_RINVNUMMSGS"      : self.dispatch_mess_tempfault,

            "ESME_RTHROTTLED"       : self.dispatch_conn_throttle,

            "ESME_RINVSCHED"        : self.dispatch_mess_permfault,
            "ESME_RINVEXPIRY"       : self.dispatch_mess_permfault,
            "ESME_RINVDFTMSGID"     : self.dispatch_mess_permfault,

            "ESME_RX_T_APPN"        : self.dispatch_mess_tempfault,

            "ESME_RX_P_APPN"        : self.dispatch_mess_permfault,
            "ESME_RX_R_APPN"        : self.dispatch_mess_permfault,
            "ESME_RQUERYFAIL"       : self.dispatch_mess_permfault,
            "ESME_RINVOPTPARSTREAM" : self.dispatch_mess_permfault,
            "ESME_ROPTPARNOTALLWD"  : self.dispatch_mess_permfault,
            "ESME_RINVPARLEN"       : self.dispatch_mess_permfault,
            "ESME_RMISSINGOPTPARAM" : self.dispatch_mess_permfault,
            "ESME_RINVOPTPARAMVAL"  : self.dispatch_mess_permfault,

            "ESME_RDELIVERYFAILURE" : self.dispatch_mess_tempfault,
            "ESME_RUNKNOWNERR"      : self.dispatch_mess_tempfault,
        }

    def command_status_dispatch(self, pdu):
        method = self.ESME_command_status_dispatch_map.get(
                pdu['header']['command_status'],
                self.dispatch_ok)
        handler = method()
        log.msg("ERROR handler:%s pdu:%s" % (handler, pdu))
        return handler

    '''This maps SMPP error states to VUMI error states
    For now assume VUMI understands:
    connection -> temp fault or permanent fault
    message -> temp fault or permanent fault
    and the need to throttle the traffic on the connection
    '''
    def dispatch_ok(self):
        return self.error_handlers.get("ok")

    def dispatch_conn_permfault(self):
        return self.error_handlers.get("conn_permfault")

    def dispatch_mess_permfault(self):
        return self.error_handlers.get("mess_permfault")

    def dispatch_conn_tempfault(self):
        return self.error_handlers.get("conn_tempfault")

    def dispatch_mess_tempfault(self):
        return self.error_handlers.get("mess_tempfault")

    def dispatch_conn_throttle(self):
        return self.error_handlers.get("conn_throttle")

    # TODO this is currently unused ... i think
    def set_handler(self, handler):
        self.handler = handler

    def update_error_handlers(self, handler_dict={}):
        self.error_handlers.update(handler_dict)

    def getSeq(self):
        return self.seq[0]

    def incSeq(self):
        self.seq[0] += self.inc

    def popData(self):
        data = None
        if(len(self.datastream) >= 16):
            command_length = int(binascii.b2a_hex(self.datastream[0:4]), 16)
            if(len(self.datastream) >= command_length):
                data = self.datastream[0:command_length]
                self.datastream = self.datastream[command_length:]
        return data

    def handleData(self, data):
        pdu = unpack_pdu(data)
        log.msg('INCOMING <<<<', binascii.b2a_hex(data))
        log.msg('INCOMING <<<<', pdu)
        error_handler = self.command_status_dispatch(pdu)
        error_handler(pdu=pdu)
        if pdu['header']['command_id'] == 'bind_transceiver_resp':
            self.handle_bind_transceiver_resp(pdu)
        if pdu['header']['command_id'] == 'submit_sm_resp':
            self.handle_submit_sm_resp(pdu)
        if pdu['header']['command_id'] == 'submit_multi_resp':
            self.handle_submit_multi_resp(pdu)
        if pdu['header']['command_id'] == 'deliver_sm':
            self.handle_deliver_sm(pdu)
        if pdu['header']['command_id'] == 'enquire_link':
            self.handle_enquire_link(pdu)
        if pdu['header']['command_id'] == 'enquire_link_resp':
            self.handle_enquire_link_resp(pdu)
        log.msg(self.name, 'STATE :', self.state)

    def loadDefaults(self, defaults):
        self.defaults = dict(self.defaults, **defaults)

    def setConnectCallback(self, connect_callback):
        self.__connect_callback = connect_callback

    def setSubmitSMRespCallback(self, submit_sm_resp_callback):
        self.__submit_sm_resp_callback = submit_sm_resp_callback

    def setDeliveryReportCallback(self, delivery_report_callback):
        self.__delivery_report_callback = delivery_report_callback

    def setDeliverSMCallback(self, deliver_sm_callback):
        self.__deliver_sm_callback = deliver_sm_callback

    def setSendFailureCallback(self, send_failure_callback):
        self._send_failure_callback = send_failure_callback

    def connectionMade(self):
        self.state = 'OPEN'
        log.msg(self.name, 'STATE :', self.state)
        pdu = BindTransceiver(self.getSeq(), **self.defaults)
        log.msg(pdu.get_obj())
        self.incSeq()
        self.sendPDU(pdu)

    def connectionLost(self, *args, **kwargs):
        self.state = 'CLOSED'
        log.msg(self.name, 'STATE :', self.state)
        try:
            self.lc_enquire.stop()
            del self.lc_enquire
            log.msg(self.name, 'stop & del enquire link looping call')
        except:
            pass
        #try:
            #self.lc_query.stop()
            #del self.lc_query
            #print self.name, 'stop & del query sm looping call'
        #except:
            #pass

    def disconnect(self):
        """
        Attempt gracefull disconnect
        """
        pass

    def forceConnectionFailure(self):
        """
        For when the tcp socket stream gets corrupted
        or something equally unrecoverable
        """
        pass

    def dataReceived(self, data):
        self.datastream += data
        data = self.popData()
        while data != None:
            self.handleData(data)
            data = self.popData()

    def sendPDU(self, pdu):
        data = pdu.get_bin()
        log.msg('OUTGOING >>>>', unpack_pdu(data))
        self.transport.write(data)

    def handle_bind_transceiver_resp(self, pdu):
        if pdu['header']['command_status'] == 'ESME_ROK':
            self.state = 'BOUND_TRX'
            self.lc_enquire = LoopingCall(self.enquire_link)
            self.lc_enquire.start(55.0)
            self.__connect_callback(self)
        log.msg(self.name, 'STATE :', self.state)

    def handle_submit_sm_resp(self, pdu):
        self.r_server.lpop("%s#unacked" % self.r_prefix)
        log.msg("%s#unacked: %s" % (
            self.r_prefix,
            self.r_server.llen("%s#unacked" % self.r_prefix)))
        message_id = pdu.get('body',{}).get('mandatory_parameters',{}).get('message_id')
        self.__submit_sm_resp_callback(
                sequence_number = pdu['header']['sequence_number'],
                command_status = pdu['header']['command_status'],
                command_id = pdu['header']['command_id'],
                message_id = message_id)
        if pdu['header']['command_status'] == 'ESME_ROK':
            pass

    def handle_submit_multi_resp(self, pdu):
        if pdu['header']['command_status'] == 'ESME_ROK':
            pass

    def _decode_message(self, message, data_coding):
        codec = {
            1: 'ascii',
            3: 'latin1',
            8: 'utf-16be',  # Actually UCS-2, but close enough.
            }.get(data_coding, None)
        if codec is None:
            log.msg("WARNING: Not decoding message with data_coding=%s" % (
                    data_coding,))
            return message
        return message.decode(codec)

    def handle_deliver_sm(self, pdu):
        if pdu['header']['command_status'] == 'ESME_ROK':
            sequence_number = pdu['header']['sequence_number']
            pdu_resp = DeliverSMResp(sequence_number, **self.defaults)
            self.sendPDU(pdu_resp)
            delivery_report = re.search( # SMPP v3.4 Issue 1.2 pg. 167 is wrong on id length
                       'id:(?P<id>\S{,65}) +sub:(?P<sub>...)'
                    +' +dlvrd:(?P<dlvrd>...)'
                    +' +submit date:(?P<submit_date>\d*)'
                    +' +done date:(?P<done_date>\d*)'
                    +' +stat:(?P<stat>[A-Z]{7})'
                    +' +err:(?P<err>...)'
                    +' +[Tt]ext:(?P<text>.{,20})'
                    +'.*',
                    pdu['body']['mandatory_parameters']['short_message'] or ''
                    )
            if delivery_report:
                self.__delivery_report_callback(
                        destination_addr = pdu['body']['mandatory_parameters']['destination_addr'],
                        source_addr = pdu['body']['mandatory_parameters']['source_addr'],
                        delivery_report = delivery_report.groupdict()
                        )
            elif detect_multipart(pdu):
                redis_key = "%s#multi_%s" % (self.r_prefix, multipart_key(detect_multipart(pdu)))
                log.msg("Redis multipart key: %s" % (redis_key))
                value = json.loads(self.r_server.get(redis_key) or 'null')
                log.msg("Retrieved value: %s" % (repr(value)))
                multi = MultipartMessage(value)
                multi.add_pdu(pdu)
                completed = multi.get_completed()
                if completed:
                    self.r_server.delete(redis_key)
                    log.msg("Re-assembled Message: %s" % (completed['message']))
                    # and we can finally pass the whole message on
                    self.__deliver_sm_callback(
                            destination_addr = completed['to_msisdn'],
                            source_addr = completed['from_msisdn'],
                            short_message = completed['message']
                            )
                else:
                    self.r_server.set(redis_key, json.dumps(multi.get_array()))
            else:
                pdu_mp = pdu['body']['mandatory_parameters']
                decoded_msg = self._decode_message(pdu_mp['short_message'],
                                                   pdu_mp['data_coding'])
                self.__deliver_sm_callback(
                        destination_addr=pdu_mp['destination_addr'],
                        source_addr=pdu_mp['source_addr'],
                        short_message=decoded_msg,
                        )

    def handle_enquire_link(self, pdu):
        if pdu['header']['command_status'] == 'ESME_ROK':
            sequence_number = pdu['header']['sequence_number']
            pdu_resp = EnquireLinkResp(sequence_number)
            self.sendPDU(pdu_resp)

    def handle_enquire_link_resp(self, pdu):
        if pdu['header']['command_status'] == 'ESME_ROK':
            pass

    def submit_sm(self, **kwargs):
        if self.state in ['BOUND_TX', 'BOUND_TRX']:
            unacked = self.r_server.llen("%s#unacked" % self.r_prefix)
            #log.msg("unacked: %s" % repr(unacked))
            # if unacked >= 1000 don't send
            # perhaps queue message for retry ?
            # that would show up in metrics, which would be good
            sequence_number = self.getSeq()
            pdu = SubmitSM(sequence_number, **dict(self.defaults, **kwargs))
            self.incSeq()
            self.sendPDU(pdu)
            self.r_server.lpush("%s#unacked" % self.r_prefix, 1)
            log.msg("%s#unacked: %s" % (
                self.r_prefix,
                self.r_server.llen("%s#unacked" % self.r_prefix)))
            return sequence_number
        return 0

    def submit_multi(self, dest_address=[], **kwargs):
        if self.state in ['BOUND_TX', 'BOUND_TRX']:
            sequence_number = self.getSeq()
            pdu = SubmitMulti(sequence_number, **dict(self.defaults, **kwargs))
            for item in dest_address:
                if isinstance(item, str): # assume strings are addresses not lists
                    pdu.addDestinationAddress(
                            item,
                            dest_addr_ton=self.defaults['dest_addr_ton'],
                            dest_addr_npi=self.defaults['dest_addr_npi'],
                            )
                elif isinstance(item, dict):
                    if item.get('dest_flag') == 1:
                        pdu.addDestinationAddress(
                                item.get('destination_addr', ''),
                                dest_addr_ton=item.get('dest_addr_ton',
                                    self.defaults['dest_addr_ton']),
                                dest_addr_npi=item.get('dest_addr_npi',
                                    self.defaults['dest_addr_npi']),
                                )
                    elif item.get('dest_flag') == 2:
                        pdu.addDistributionList(item.get('dl_name'))
            self.incSeq()
            self.sendPDU(pdu)
            return sequence_number
        return 0

    def enquire_link(self, **kwargs):
        if self.state in ['BOUND_TX', 'BOUND_TRX']:
            sequence_number = self.getSeq()
            pdu = EnquireLink(sequence_number, **dict(self.defaults, **kwargs))
            self.incSeq()
            self.sendPDU(pdu)
            return sequence_number
        return 0

    def query_sm(self, message_id, source_addr, **kwargs):
        if self.state in ['BOUND_TX', 'BOUND_TRX']:
            sequence_number = self.getSeq()
            pdu = QuerySM(sequence_number,
                    message_id=message_id,
                    source_addr=source_addr,
                    **dict(self.defaults, **kwargs))
            self.incSeq()
            self.sendPDU(pdu)
            return sequence_number
        return 0


class EsmeTransceiverFactory(ReconnectingClientFactory):

    def __init__(self, config, vumi_options):
        self.config = config
        self.vumi_options = vumi_options
        if int(self.config['smpp_increment']) < int(self.config['smpp_offset']):
            raise Exception("increment may not be less than offset")
        if int(self.config['smpp_increment']) < 1:
            raise Exception("increment may not be less than 1")
        if int(self.config['smpp_offset']) < 1:
            raise Exception("offset may not be less than 1")
        self.esme = None
        self.__connect_callback = None
        self.__disconnect_callback = None
        self.__submit_sm_resp_callback = None
        self.__delivery_report_callback = None
        self.__deliver_sm_callback = None
        self.seq = [int(self.config['smpp_offset'])]
        log.msg("Set sequence number: %s, config: %s" % (self.seq, self.config))
        self.initialDelay = 30.0
        self.maxDelay = 45
        self.defaults = {
                'host': '127.0.0.1',
                'port': 2775,
                'dest_addr_ton': 0,
                'dest_addr_npi': 0,
                }

    def loadDefaults(self, defaults):
        self.defaults = dict(self.defaults, **defaults)

    def setLatestSequenceNumber(self, latest):
        self.seq = [latest]
        log.msg("Set sequence number: %s, config: %s" % (
            self.seq, self.config))

    def setConnectCallback(self, connect_callback):
        self.__connect_callback = connect_callback

    def setDisconnectCallback(self, disconnect_callback):
        self.__disconnect_callback = disconnect_callback

    def setSubmitSMRespCallback(self, submit_sm_resp_callback):
        self.__submit_sm_resp_callback = submit_sm_resp_callback

    def setDeliveryReportCallback(self, delivery_report_callback):
        self.__delivery_report_callback = delivery_report_callback

    def setDeliverSMCallback(self, deliver_sm_callback):
        self.__deliver_sm_callback = deliver_sm_callback

    def setSendFailureCallback(self, send_failure_callback):
        self._send_failure_callback = send_failure_callback

    def startedConnecting(self, connector):
        print 'Started to connect.'

    def buildProtocol(self, addr):
        print 'Connected'
        self.esme = EsmeTransceiver(self.seq, self.config, self.vumi_options)
        self.esme.loadDefaults(self.defaults)
        self.esme.setConnectCallback(
                connect_callback=self.__connect_callback)
        self.esme.setSubmitSMRespCallback(
                submit_sm_resp_callback=self.__submit_sm_resp_callback)
        self.esme.setDeliveryReportCallback(
                delivery_report_callback=self.__delivery_report_callback)
        self.esme.setDeliverSMCallback(
                deliver_sm_callback=self.__deliver_sm_callback)
        self.resetDelay()
        return self.esme

    def clientConnectionLost(self, connector, reason):
        print 'Lost connection.  Reason:', reason
        self.__disconnect_callback()
        ReconnectingClientFactory.clientConnectionLost(
                self, connector, reason)

    def clientConnectionFailed(self, connector, reason):
        print 'Connection failed. Reason:', reason
        ReconnectingClientFactory.clientConnectionFailed(
                self, connector, reason)

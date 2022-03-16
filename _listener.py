sloc)  13.6 KB
import time
import binascii
import struct

from twisted.internet import defer

from stratum.services import GenericService
from stratum.pubsub import Pubsub, Subscription
from stratum.custom_exceptions import ServiceException, RemoteServiceException

from jobs import JobRegistry

import stratum.logger
log = stratum.logger.get_logger('proxy')

def var_int(i):
    if i <= 0xff:
        return struct.pack('>B', i)
    elif i <= 0xffff:
        return struct.pack('>H', i)
    raise Exception("number is too big")

class UpstreamServiceException(ServiceException):
    code = -2

class SubmitException(ServiceException):
    code = -2

class DifficultySubscription(Subscription):
    event = 'mining.set_difficulty'
    difficulty = 1
    
    @classmethod
    def on_new_difficulty(cls, new_difficulty):
        cls.difficulty = new_difficulty
        cls.emit(new_difficulty)
    
    def after_subscribe(self, *args):
        self.emit_single(self.difficulty)
        
class MiningSubscription(Subscription):
    '''This subscription object implements
    logic for broadcasting new jobs to the clients.'''
    
    event = 'mining.notify'
    
    last_broadcast = None
    
    @classmethod
    def disconnect_all(cls):
        for subs in Pubsub.iterate_subscribers(cls.event):
            if subs.connection_ref().transport != None:
                subs.connection_ref().transport.loseConnection()
        
    @classmethod
    def on_template(cls, job_id, prevhash, coinb1, coinb2, merkle_branch, version, nbits, ntime, clean_jobs):
        '''Push new job to subscribed clients'''
        cls.last_broadcast = (job_id, prevhash, coinb1, coinb2, merkle_branch, version, nbits, ntime, clean_jobs)
        cls.emit(job_id, prevhash, coinb1, coinb2, merkle_branch, version, nbits, ntime, clean_jobs)
        
    def _finish_after_subscribe(self, result):
        '''Send new job to newly subscribed client'''
        try:        
            (job_id, prevhash, coinb1, coinb2, merkle_branch, version, nbits, ntime, _) = self.last_broadcast
        except Exception:
            log.error("Template not ready yet")
            return result
        
        self.emit_single(job_id, prevhash, coinb1, coinb2, merkle_branch, version, nbits, ntime, True)
        return result
             
    def after_subscribe(self, *args):
        '''This will send new job to the client *after* he receive subscription details.
        on_finish callback solve the issue that job is broadcasted *during*
        the subscription request and client receive messages in wrong order.'''
        self.connection_ref().on_finish.addCallback(self._finish_after_subscribe)

class VersionMaskSubscription(Subscription):
    """Each instance represents per connection version mask subscription
    Note: mask may differ per connection depending on downstream configuration
    requests
    """
    event = 'mining.set_version_mask'
    # Default mask as specified by the BIP
    version_mask = 0
    #  By default, version rolling is not enabled. Therefore, no subscription is
    #  possible
    enabled = False

    def __init__(self, subscriber_version_mask, **params):
        super(VersionMaskSubscription, self).__init__(**params)
        self.subscriber_version_mask = subscriber_version_mask

    @classmethod
    def on_new_mask(cls, version_mask):
        """
        :param version_mask: new mask from upstream
        """
        cls.version_mask = version_mask
        cls.emit()

    def get_effective_mask(self):
        """Calculates effective version rolling mask
        :return: combined masked based on mask requested by the miner
        and mask provided by the upstream connection
        allowed mask
        """
        return self.version_mask & self.subscriber_version_mask

    def after_subscribe(self, *args):
        self.emit_single()

    def process(self, *args, **kwargs):
        return ['%x' % self.get_effective_mask()]

    @classmethod
    def enable(cls):
        cls.enabled = True


class StratumExtension(object):
    @classmethod
    def configure_downstream(cls, connection_ref, values):
        """Default implementation that generates downstream extension response
        :param connection_ref: reference to the connection that demanded the
        extension
        :param values: values for the extension
        :return: dictionary that indicates the extension is not available
        """
        return {cls.namespace: False}

    @classmethod
    def get_values(cls):
        pass

class VersionRollingExtension(StratumExtension):
    """Version rolling extension
    This class provides version rolling extension parameter values when
    negotiating with upstream pool. It also provides combined version
    rolling mask for downstream miners based on previous negotiation with the
    pool.
    """
    namespace = 'version-rolling'

    @classmethod
    def _get_mask_from_values(cls, values):
        """Extracts version mask from values dictionary
        :param values: a dictinary with all extensions
        :return: mask or throws an exception
        """
        mask_str = values[cls.namespace + '.mask']
        mask = int(mask_str, 16)
        return mask

    @classmethod
    def configure_downstream(cls, connection_ref, values):
        """Version rolling configuration for downstream miners
        :param connection_ref:
        :param values:
        :return:
        :todo consider whether the result should indicate version rolling
        disabled in case there is no intersection between the upstream mask
        and the downstream requested mask
        """
        result = super(VersionRollingExtension, cls).configure_downstream(
            connection_ref, values)

        try:
            downstream_proposed_mask = cls._get_mask_from_values(values)
            # Only when subscription is enabled compose a valid mask
            if VersionMaskSubscription.enabled:
                sub = VersionMaskSubscription(downstream_proposed_mask)
                result = {cls.namespace: True,
                          cls.namespace + '.mask': '%x' % sub.get_effective_mask()}
                _ = Pubsub.subscribe(connection_ref(), sub)
        except Exception as e:
            log.error("Version mask not calculated, error: '%s'" % e)

        return result

    @classmethod
    def prepare_upstream(cls, args):
        """Prepares version rolling extension for upstream
        :param args: object with command line arguments
        :return: a tuple - extension name and a dictionary with extension
        parameter values
        """
        values = {
            cls.namespace + '.mask': '%x' % args.version_rolling_mask,
            cls.namespace + '.min-bit-count':
                args.version_rolling_min_bit_count,
        }
        return cls.namespace, values

    @classmethod
    def configure_upstream(cls, values):
        """Configure version rolling extension based on upstream response
        A valid mask parsed from the upstream values is provided for
        subscription only if it is non-zero
        :param values: dictionary with all extensions
        :return: nothing
        """
        if values.get(cls.namespace, False):
            try:
                upstream_proposed_mask = cls._get_mask_from_values(values)
            except Exception as e:
                log.error("Cannot parse upstream version mask error: '%s'" % e)
            else:
                if upstream_proposed_mask != 0:
                    VersionMaskSubscription.enable()
                    VersionMaskSubscription.on_new_mask(upstream_proposed_mask)
                    log.info('Version rolling stratum extension enabled (mask: '
                             '%x)' %
                             upstream_proposed_mask)


extensions = {
    VersionRollingExtension.namespace: VersionRollingExtension
}


class StratumProxyService(GenericService):
    service_type = 'mining'
    service_vendor = 'mining_proxy'
    is_default = True
    
    _f = None # Factory of upstream Stratum connection
    custom_user = None
    custom_password = None
    extranonce1 = None
    extranonce2_size = None
    tail_iterator = 0
    registered_tails= []
    
    @classmethod
    def _set_upstream_factory(cls, f):
        cls._f = f

    @classmethod
    def _set_custom_user(cls, custom_user, custom_password):
        cls.custom_user = custom_user
        cls.custom_password = custom_password
        
    @classmethod
    def _set_extranonce(cls, extranonce1, extranonce2_size):
        cls.extranonce1 = extranonce1
        cls.extranonce2_size = extranonce2_size
        
    @classmethod
    def _get_unused_tail(cls):
        '''Currently adds up to two bytes to extranonce1,
        limiting proxy for up to 65535 connected clients.'''
        
        for _ in range(0, 0xffff):  # 0-65535
            cls.tail_iterator += 1
            cls.tail_iterator %= 0xffff

            # Zero extranonce is reserved for getwork connections
            if cls.tail_iterator == 0:
                cls.tail_iterator += 1

            # var_int throws an exception when input is >= 0xffff
            tail = var_int(cls.tail_iterator)
            tail_len = len(tail)

            if tail not in cls.registered_tails:
                cls.registered_tails.append(tail)
                return (binascii.hexlify(tail), cls.extranonce2_size - tail_len)
            
        raise Exception("Extranonce slots are full, please disconnect some miners!")
    
    def _drop_tail(self, result, tail):
        tail = binascii.unhexlify(tail)
        if tail in self.registered_tails:
            self.registered_tails.remove(tail)
        else:
            log.error("Given extranonce is not registered1")
        return result
            
    @defer.inlineCallbacks
    def authorize(self, worker_name, worker_password, *args):
        if self._f.client == None or not self._f.client.connected:
            yield self._f.on_connect

        if self.custom_user != None:
            # Already subscribed by main()
            defer.returnValue(True)
                        
        result = (yield self._f.rpc('mining.authorize', [worker_name, worker_password]))
        defer.returnValue(result)

    @defer.inlineCallbacks
    def configure(self, extensions_names, extensions_values, *args):
        """Handles configure method from downstream connections
        A list of extensions is matched against a list of supported extensions.
        When a matching extension is found it is provided with the parameter
        values from downstream and a resulting configuration is collected.
        :param extensions_names:
        :param extensions_values:
        :param args:
        :return:
        """
        if self._f.client == None or not self._f.client.connected:
            yield self._f.on_connect

        config_result = {}
        for ext_name in extensions_names:
            ext = extensions.get(ext_name, None)
            if ext is not None:
                config_result.update(ext.configure_downstream(
                    self.connection_ref, extensions_values))

        defer.returnValue(config_result)

    @defer.inlineCallbacks
    def subscribe(self, *args):    
        if self._f.client == None or not self._f.client.connected:
            yield self._f.on_connect
            
        if self._f.client == None or not self._f.client.connected:
            raise UpstreamServiceException("Upstream not connected")
         
        if self.extranonce1 == None:
            # This should never happen, because _f.on_connect is fired *after*
            # connection receive mining.subscribe response
            raise UpstreamServiceException("Not subscribed on upstream yet")
        
        (tail, extranonce2_size) = self._get_unused_tail()
        
        session = self.connection_ref().get_session()
        session['tail'] = tail
                
        # Remove extranonce from registry when client disconnect
        self.connection_ref().on_disconnect.addCallback(self._drop_tail, tail)

        subs1 = Pubsub.subscribe(self.connection_ref(), DifficultySubscription())[0]
        subs2 = Pubsub.subscribe(self.connection_ref(), MiningSubscription())[0]            
        defer.returnValue(((subs1, subs2),) + (self.extranonce1+tail, extranonce2_size))
            
    @defer.inlineCallbacks
    def submit(self, worker_name, job_id, extranonce2, ntime, nonce, *args):
        if self._f.client == None or not self._f.client.connected:
            raise SubmitException("Upstream not connected")

        session = self.connection_ref().get_session()
        tail = session.get('tail')
        if tail == None:
            raise SubmitException("Connection is not subscribed")

        if self.custom_user:
            worker_name = self.custom_user

        start = time.time()
        submit_params = [worker_name, job_id, tail+extranonce2, ntime, nonce]
        # A simplified way of detecting version rolling is that we have
        # received an additional version field and there is version mask
        # subscription enabled
        if len(args) == 1 and VersionMaskSubscription.enabled:
            submit_params.append(args[0])
        try:
            result = (yield self._f.rpc('mining.submit', submit_params))

        except RemoteServiceException as exc:
            response_time = (time.time() - start) * 1000
            log.info("[%dms] Share from '%s' REJECTED: %s" % (response_time, worker_name, str(exc)))
            raise SubmitException(*exc.args)

        response_time = (time.time() - start) * 1000
        log.info("[%dms] Share from '%s' accepted, diff %d" % (response_time, worker_name, DifficultySubscription.difficulty))
        defer.returnValue(result)

    def get_transactions(self, *args):
        log.warn("mining.get_transactions isn't supported by proxy")
        return []

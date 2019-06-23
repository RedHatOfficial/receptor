import base64
import datetime
import json
import logging
import uuid

logger = logging.getLogger(__name__)


class OuterEnvelope:
    def __init__(self, frame_id, sender, recipient, route_list, inner):
        self.frame_id = frame_id
        self.sender = sender
        self.recipient = recipient
        self.route_list = route_list
        self.inner = inner
        self.inner_obj = None

    async def deserialize_inner(self, receptor):
        self.inner_obj = await InnerEnvelope.deserialize(receptor, self.inner)
        return self.inner_obj

    @classmethod
    def from_raw(cls, raw):
        doc = json.loads(raw)
        return cls(**doc)
    
    def serialize(self):
        return json.dumps(dict(
            frame_id=self.frame_id,
            sender=self.sender,
            recipient=self.recipient,
            route_list=self.route_list,
            inner=self.inner
        ))


class Directive(object):
    def __init__(self, namespace, action):
        self.namespace = namespace
        self.action = action

    @classmethod
    def from_str(cls, s):
        namespace, action = s.split(":", 1)
        return cls(namespace=namespace, action=action)

    def __str__(self):
        return f"{self.namespace}:{self.action}"


class InnerEnvelope:
    def __init__(self, receptor, message_id, sender, recipient, message_type, timestamp,
                 raw_payload, directive=None, in_response_to=None, ttl=None,
                 serial=1):
        self.receptor = receptor
        self.message_id = message_id
        self.sender = sender
        self.recipient = recipient
        self.message_type = message_type # 'directive' or 'response'
        self.timestamp = timestamp # ISO format
        self.raw_payload = raw_payload
        self._directive = directive # None if response, 'namespace:action' if not
        self.in_response_to = in_response_to # None if directive, a message_id if not
        self.ttl = ttl # Optional
        self.serial = serial # serial index of responses

    @property
    def directive(self):
        return Directive.from_str(self._directive)

    @directive.setter
    def directive(self, s):
        self._directive = s

    @classmethod
    async def deserialize(cls, receptor, msg):
        payload = await receptor.config.components.security_manager.verify_msg(msg)
        # validate msg
        # msg+sig
        return cls(receptor=receptor, **json.loads(payload))

    @classmethod
    def make_response(cls, receptor, recipient, payload, in_response_to, serial, ttl=None):
        if isinstance(payload, bytes):
            encoded_payload = base64.encodebytes(payload)
        else:
            encoded_payload = payload
        return cls(
            receptor=receptor,
            message_id=str(uuid.uuid4()),
            sender=receptor.node_id,
            recipient=recipient,
            message_type='response',
            timestamp=datetime.datetime.utcnow().isoformat(),
            raw_payload=encoded_payload,
            directive=None,
            in_response_to=in_response_to,
            ttl=ttl,
            serial=serial
        )

    def sign_and_serialize(self):
        return self.receptor.config.components.security_manager.sign_response(self)

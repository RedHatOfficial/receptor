import asyncio
import datetime
import io
import logging
import os
from contextlib import suppress

from .connection.base import Worker
from .connection.manager import Manager
from .diagnostics import status
from .messages.framed import FileBackedBuffer, FramedMessage
from .receptor import Receptor
from .control_socket import ControlSocketServer

logger = logging.getLogger(__name__)


class Controller:
    """
    This class is the mechanism by which a larger system would interface with the Receptor
    mesh as a Controller. For more details on writing Controllers see :ref:`controller` Good
    examples of its usage can be found in :mod:`receptor.entrypoints`

    :param config: Overall Receptor configuration
    :param loop: An asyncio eventloop, if not provided the current event loop will be fetched
    :param queue: A queue that responses will be placed into as they are received

    :type config: :class:`receptor.config.ReceptorConfig`
    :type loop: asyncio event loop
    :type queue: asyncio.Queue
    """

    def __init__(self, config, loop=asyncio.get_event_loop()):
        self.receptor = Receptor(config)
        self.loop = loop
        self.connection_manager = Manager(
            lambda: Worker(self.receptor, loop), self.receptor.config.get_ssl_context, loop
        )
        self.status_task = loop.create_task(status(self.receptor))

    async def shutdown_loop(self):
        tasks = [
            task for task in asyncio.Task.all_tasks() if task is not asyncio.Task.current_task()
        ]
        # Retrieve and throw away all exceptions that happen after
        # the decision to shut down was made.
        for task in tasks:
            task.cancel()
            with suppress(Exception):
                await task
        await asyncio.gather(*tasks)
        self.loop.stop()

    async def exit_on_exceptions_in(self, tasks):
        try:
            for task in tasks:
                await task
        except Exception as e:
            logger.exception(str(e))
            self.loop.create_task(self.shutdown_loop())

    def enable_server(self, listen_urls):
        """
        Enables a listening server to receive *Peer* connections, multiple servers can be started.

        Examples of supported formats:

        * rnps://0.0.0.0:7323 - Secure receptor protocol bound on all interfaces port 7323
        * rnp://1.2.3.4:7323 - Insecure receptor protocol bound to the interface of 1.2.3.4
          port 7323
        * wss://0.0.0.0:443 - Secure websocket protocol bound on all interfaces port 443

        The services are started as asyncio tasks and will start listening once
        :meth:`receptor.controller.Controller.run` is called.

        :param listen_urls: A list of listener urls
        """
        tasks = list()
        for url in listen_urls:
            logger.info("Serving on %s", url)
            listener = self.connection_manager.get_listener(url)
            tasks.append(self.loop.create_task(listener))
        return tasks

    def enable_control_socket(self, socket_path):
        """
        Enables a Unix domain socket over which the running node can receive commands such as send,
        ping and status.

        The socket listener is started as an asyncio task and will start listening once
        :meth:`receptor.controller.Controller.run` is called.

        :param socket_path: A path to the socket file, such as /var/run/receptor.sock.
        """
        logger.info("Listening on Unix socket on %s", socket_path)
        socket_server = asyncio.start_unix_server(
            ControlSocketServer(self).serve_from_socket, path=socket_path, loop=self.loop
        )
        return self.loop.create_task(socket_server)

    def add_peer(self, peer, ws_extra_headers=None, ws_heartbeat=None):
        """
        Adds a Receptor Node *Peer*. A connection will be established to this node once
        :meth:`receptor.controller.Controller.run` is called.

        Example format:
        rnps://10.0.1.1:7323

        :param peer: remote peer url
        """
        logger.info("Connecting to peer {}".format(peer))
        return self.connection_manager.get_peer(
            peer, ws_extra_headers=ws_extra_headers, ws_heartbeat=ws_heartbeat,
        )

    async def send(self, payload, recipient, directive, response_handler=None):
        """
        Sends a payload to a recipient *Node* to execute under a given *directive*.

        This method is intended to take these inputs and craft a
        :class:`receptor.messages.framed.FramedMessage` that can then be sent along to the mesh.

        The payload input type is highly flexible and intends on getting out of the way of the
        contract made between the producer/sender of the data and the plugin on the destination
        node that is intended on executing it. As such the payload data type can be one of:

        * A file path
        * str, or bytes - Strings will be converted to bytes before transmission
        * dict - This will be serialized to json before transmission
        * io.BytesIO - This can be any type that is based on *io.BytesIO* and  supports read()

        The *directive* should be a string and take the form of ``<plugin>:<method`` for example,
        the `Receptor HTTP Plugin <https://github.com/project-receptor/receptor-http>`_ would take
        the form of ``receptor-http:execute``

        This method returns a message identifier, that message identifier can be used to reference
        responses returned from the plugin as having originated from the message sent by this
        request.

        :param payload: See above
        :param recipient: The node id of a Receptor Node on the mesh
        :param directive: See above
        :param expect_response: Optional Whether it is expected that the plugin will emit a
            response.

        :return: a message-id that can be used to reference responses
        """
        if os.path.exists(payload):
            buffer = FileBackedBuffer.from_path(payload)
        elif isinstance(payload, (str, bytes)):
            buffer = FileBackedBuffer.from_data(payload)
        elif isinstance(payload, dict):
            buffer = FileBackedBuffer.from_dict(payload)
        elif isinstance(payload, io.BytesIO):
            buffer = FileBackedBuffer.from_buffer(payload)
        message = FramedMessage(
            header=dict(
                sender=self.receptor.node_id,
                recipient=recipient,
                timestamp=datetime.datetime.utcnow(),
                directive=directive,
            ),
            payload=buffer,
        )
        await self.receptor.router.send(message, response_handler=response_handler)
        return message.msg_id

    async def ping(self, destination, response_handler=None):
        """
        Sends a ping message to a remote Receptor node with the expectation that it will return
        information about when it received the ping, what its capabilities are and what work it
        is currently doing.

        :param destination: The node id of the target node
        :param response_handler: Callback function to receive the ping response
        :returns: a message-id that can be used to reference responses
        """
        return await self.receptor.router.ping_node(destination, response_handler)

    def run(self, app=None):
        """
        Starts the Controller's event loop, this method will not return until the event loop is
        stopped. An optional async function can be given, This will cause the Controller's event
        loop to run until that function returns.

        :param app: optional; async function that will run and shut the loop down when it returns
        """
        try:
            if app is None:
                app = self.receptor.shutdown_handler
            self.loop.run_until_complete(app())
        except KeyboardInterrupt:
            pass
        finally:
            self.loop.stop()

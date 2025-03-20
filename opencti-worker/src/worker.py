# coding: utf-8


import base64
import ctypes
import datetime
import functools
import json
import os
import random
import sys
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from threading import Thread
from typing import Any, Dict, List, Optional, Union

import pika
import requests
import yaml
from opentelemetry import metrics
from opentelemetry.exporter.prometheus import PrometheusMetricReader
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.resources import SERVICE_NAME, Resource
from pika.adapters.blocking_connection import BlockingChannel
from prometheus_client import start_http_server
from pycti import OpenCTIApiClient, OpenCTIStix2Splitter, __version__
from pycti.connector.opencti_connector_helper import (
    create_mq_ssl_context,
    get_config_variable,
)
from pycti.utils.opencti_logger import logger
from requests import RequestException, Timeout

ERROR_TYPE_BAD_GATEWAY = "Bad Gateway"
ERROR_TYPE_TIMEOUT = "Request timed out"

# Telemetry variables definition
meter = metrics.get_meter(__name__)
resource = Resource(attributes={SERVICE_NAME: "opencti-worker"})
bundles_global_counter = meter.create_counter(
    name="opencti_bundles_global_counter",
    description="number of bundles processed",
)
bundles_processing_time_gauge = meter.create_histogram(
    name="opencti_bundles_processing_time_gauge",
    description="processing time of bundles",
)


class PingAlive(threading.Thread):
    def __init__(self, worker_logger, api) -> None:
        threading.Thread.__init__(self)
        self.worker_logger = worker_logger
        self.api = api
        self.exit_event = threading.Event()

    def ping(self) -> None:
        while not self.exit_event.is_set():
            try:
                self.worker_logger.debug("PingAlive running.")
                self.api.query(
                    """
                    query workerPing {
                      about {
                        version
                      }
                    }
                  """
                )
            except Exception as e:  # pylint: disable=broad-except
                self.in_error = True
                self.worker_logger.error(
                    "Error pinging the API",
                    {"reason": str(e), "headers": str(self.api.get_request_headers())},
                )
            self.exit_event.wait(30)

    def run(self) -> None:
        self.worker_logger.info("Starting PingAlive thread")
        self.ping()

    def stop(self) -> None:
        self.worker_logger.info("Preparing PingAlive for clean shutdown")
        self.exit_event.set()


@dataclass(unsafe_hash=True)
class ApiConsumer(Thread):  # pylint: disable=too-many-instance-attributes
    execution_pool: ThreadPoolExecutor
    connector: Dict[str, Any] = field(hash=False)
    config: Dict[str, Any] = field(hash=False)
    opencti_token: str
    listen_api_ssl_verify: bool
    listen_api_http_proxy: str
    listen_api_https_proxy: str
    log_level: str = "info"
    json_logging: bool = True

    def __post_init__(self) -> None:
        super().__init__()
        self.logger_class = logger(self.log_level.upper(), self.json_logging)
        self.worker_logger = self.logger_class("worker")

        self.queue_name = self.connector["config"]["listen"]
        self.pika_credentials = pika.PlainCredentials(
            self.connector["config"]["connection"]["user"],
            self.connector["config"]["connection"]["pass"],
        )
        ssl_options = None
        if self.connector["config"]["connection"]["use_ssl"]:
            ssl_options = pika.SSLOptions(
                create_mq_ssl_context(self.config),
                self.connector["config"]["connection"]["host"],
            )

        self.pika_parameters = pika.ConnectionParameters(
            self.connector["config"]["connection"]["host"],
            self.connector["config"]["connection"]["port"],
            self.connector["config"]["connection"]["vhost"],
            self.pika_credentials,
            ssl_options=ssl_options,
        )
        self.pika_connection = pika.BlockingConnection(self.pika_parameters)
        self.channel = self.pika_connection.channel()
        try:
            self.channel.confirm_delivery()
        except Exception as err:  # pylint: disable=broad-except
            self.worker_logger.warning(str(err))
        self.channel.basic_qos(prefetch_count=1)
        assert self.channel is not None
        self.current_bundle_id: [str, None] = None
        self.current_bundle_seq: int = 0

    @property
    def id(self) -> Any:  # pylint: disable=inconsistent-return-statements
        if hasattr(self, "_thread_id"):
            return self._thread_id  # type: ignore  # pylint: disable=no-member
        # pylint: disable=protected-access
        for id_, thread in threading._active.items():  # type: ignore
            if thread is self:
                return id_

    def terminate(self) -> None:
        thread_id = self.id
        res = ctypes.pythonapi.PyThreadState_SetAsyncExc(
            thread_id, ctypes.py_object(SystemExit)
        )
        if res > 1:
            ctypes.pythonapi.PyThreadState_SetAsyncExc(thread_id, 0)
            self.worker_logger.info("Unable to kill the thread")

    def nack_message(self, channel: BlockingChannel, delivery_tag: int) -> None:
        if channel.is_open:
            self.worker_logger.info("Message rejected", {"tag": delivery_tag})
            channel.basic_nack(delivery_tag)
        else:
            self.worker_logger.info(
                "Message NOT rejected (channel closed)", {"tag": delivery_tag}
            )

    def ack_message(self, channel: BlockingChannel, delivery_tag: int) -> None:
        if channel.is_open:
            self.worker_logger.info("Message acknowledged", {"tag": delivery_tag})
            channel.basic_ack(delivery_tag)
        else:
            self.worker_logger.info(
                "Message NOT acknowledged (channel closed)", {"tag": delivery_tag}
            )

    def stop_consume(self, channel: BlockingChannel) -> None:
        if channel.is_open:
            channel.stop_consuming()

    # Callable for consuming a message
    def _process_message(
        self,
        channel: BlockingChannel,
        method: Any,
        properties: None,  # pylint: disable=unused-argument
        body: str,
    ) -> None:
        self.worker_logger.info(
            "Processing a new message, launching a thread...",
            {"tag": method.delivery_tag},
        )
        task_future = self.execution_pool.submit(
            self.data_handler, self.pika_connection, channel, method.delivery_tag, body
        )
        while task_future.running():  # Loop while the thread is processing
            self.pika_connection.sleep(0.05)
        self.worker_logger.info("Message processed, thread terminated")

    # Data handling
    def data_handler(  # pylint: disable=too-many-statements, too-many-locals
        self,
        connection: Any,
        channel: BlockingChannel,
        delivery_tag: str,
        data: str,
    ) -> Optional[bool]:
        try:
            callback_uri = self.connector["config"].get("listen_callback_uri")
            request_headers = {
                "User-Agent": "pycti/" + __version__,
                "Authorization": "Bearer " + self.opencti_token,
            }
            requests.post(
                callback_uri,
                data=data,
                headers=request_headers,
                verify=self.listen_api_ssl_verify,
                proxies={
                    "http": self.listen_api_http_proxy,
                    "https": self.listen_api_https_proxy,
                },
                timeout=300,
            )
            # Ack the message
            cb = functools.partial(self.ack_message, channel, delivery_tag)
            connection.add_callback_threadsafe(cb)
        except (RequestException, Timeout):
            self.worker_logger.warning("A connection error or timeout occurred")
            # Platform is under heavy load: wait for unlock & retry almost indefinitely.
            sleep_jitter = round(random.uniform(10, 30), 2)
            time.sleep(sleep_jitter)
            # Nack the message
            cb = functools.partial(self.nack_message, channel, delivery_tag)
            connection.add_callback_threadsafe(cb)
        except Exception as ex:
            # Technical unmanaged exception
            self.worker_logger.error(
                "Error executing listen handling", {"reason": str(ex)}
            )
            error_msg = traceback.format_exc()
            if ERROR_TYPE_BAD_GATEWAY in error_msg or ERROR_TYPE_TIMEOUT in error_msg:
                # Nack the message
                cb = functools.partial(self.nack_message, channel, delivery_tag)
                connection.add_callback_threadsafe(cb)
            else:
                # Technical error, log and continue
                # Ack the message
                cb = functools.partial(self.ack_message, channel, delivery_tag)
                connection.add_callback_threadsafe(cb)

    def run(self) -> None:
        try:
            # Consume the queue
            self.worker_logger.info(
                "Thread for listen queue started", {"queue": self.queue_name}
            )
            self.channel.basic_consume(
                queue=self.queue_name,
                on_message_callback=self._process_message,
            )
            self.channel.start_consuming()
        finally:
            self.channel.stop_consuming()
            self.worker_logger.info(
                "Thread for listen queue terminated", {"queue": self.queue_name}
            )


@dataclass(unsafe_hash=True)
class Consumer(Thread):  # pylint: disable=too-many-instance-attributes
    execution_pool: ThreadPoolExecutor
    connector: Dict[str, Any] = field(hash=False)
    config: Dict[str, Any] = field(hash=False)
    opencti_url: str
    opencti_token: str
    log_level: str
    ssl_verify: Union[bool, str] = False
    json_logging: bool = True

    def __post_init__(self) -> None:
        super().__init__()
        self.api = OpenCTIApiClient(
            url=self.opencti_url,
            token=self.opencti_token,
            log_level=self.log_level,
            ssl_verify=self.ssl_verify,
            json_logging=self.json_logging,
        )
        self.worker_logger = self.api.logger_class("worker")

        # Start ping
        self.ping = PingAlive(self.worker_logger, self.api)
        self.ping.start()

        self.queue_name = self.connector["config"]["push"]
        self.pika_credentials = pika.PlainCredentials(
            self.connector["config"]["connection"]["user"],
            self.connector["config"]["connection"]["pass"],
        )
        ssl_options = None
        if self.connector["config"]["connection"]["use_ssl"]:
            ssl_options = pika.SSLOptions(
                create_mq_ssl_context(self.config),
                self.connector["config"]["connection"]["host"],
            )

        self.pika_parameters = pika.ConnectionParameters(
            self.connector["config"]["connection"]["host"],
            self.connector["config"]["connection"]["port"],
            self.connector["config"]["connection"]["vhost"],
            self.pika_credentials,
            ssl_options=ssl_options,
        )
        self.pika_connection = pika.BlockingConnection(self.pika_parameters)
        self.channel = self.pika_connection.channel()
        try:
            self.channel.confirm_delivery()
        except Exception as err:  # pylint: disable=broad-except
            self.worker_logger.warning(str(err))
        self.channel.basic_qos(prefetch_count=1)
        assert self.channel is not None
        self.current_bundle_id: [str, None] = None
        self.current_bundle_seq: int = 0

    @property
    def id(self) -> Any:  # pylint: disable=inconsistent-return-statements
        if hasattr(self, "_thread_id"):
            return self._thread_id  # type: ignore  # pylint: disable=no-member
        # pylint: disable=protected-access
        for id_, thread in threading._active.items():  # type: ignore
            if thread is self:
                return id_

    def terminate(self) -> None:
        thread_id = self.id
        self.ping.stop()
        res = ctypes.pythonapi.PyThreadState_SetAsyncExc(
            thread_id, ctypes.py_object(SystemExit)
        )
        if res > 1:
            ctypes.pythonapi.PyThreadState_SetAsyncExc(thread_id, 0)
            self.worker_logger.info("Unable to kill the thread")

    def nack_message(self, channel: BlockingChannel, delivery_tag: int) -> None:
        if channel.is_open:
            self.worker_logger.info("Message rejected", {"tag": delivery_tag})
            channel.basic_nack(delivery_tag)
        else:
            self.worker_logger.info(
                "Message NOT rejected (channel closed)", {"tag": delivery_tag}
            )

    def ack_message(self, channel: BlockingChannel, delivery_tag: int) -> None:
        if channel.is_open:
            self.worker_logger.info("Message acknowledged", {"tag": delivery_tag})
            channel.basic_ack(delivery_tag)
        else:
            self.worker_logger.info(
                "Message NOT acknowledged (channel closed)", {"tag": delivery_tag}
            )

    def stop_consume(self, channel: BlockingChannel) -> None:
        self.ping.stop()
        if channel.is_open:
            channel.stop_consuming()

    # Callable for consuming a message
    def _process_message(
        self,
        channel: BlockingChannel,
        method: Any,
        properties: None,  # pylint: disable=unused-argument
        body: str,
    ) -> None:
        data = json.loads(body)
        self.worker_logger.info(
            "Processing a new message, launching a thread...",
            {"tag": method.delivery_tag},
        )
        task_future = self.execution_pool.submit(
            self.data_handler, self.pika_connection, channel, method.delivery_tag, data
        )
        while task_future.running():  # Loop while the thread is processing
            self.pika_connection.sleep(0.05)
        self.worker_logger.info("Message processed, thread terminated")

    # Data handling
    def data_handler(  # pylint: disable=too-many-statements, too-many-locals
        self,
        connection: Any,
        channel: BlockingChannel,
        delivery_tag: str,
        data: Dict[str, Any],
    ) -> Optional[bool]:
        start_processing = datetime.datetime.now()
        # Set the API headers
        self.api.set_applicant_id_header(data.get("applicant_id"))
        self.api.set_playbook_id_header(data.get("playbook_id"))
        self.api.set_event_id(data.get("event_id"))
        self.api.set_draft_id(data.get("draft_id"))
        work_id = data["work_id"] if "work_id" in data else None
        synchronized = data["synchronized"] if "synchronized" in data else False
        self.api.set_synchronized_upsert_header(synchronized)
        previous_standard = data.get("previous_standard")
        self.api.set_previous_standard_header(previous_standard)
        # Execute the import
        imported_items = []
        event_type = data["type"] if "type" in data else "bundle"
        types = (
            data["entities_types"]
            if "entities_types" in data and len(data["entities_types"]) > 0
            else None
        )
        try:
            if event_type == "bundle":
                content = base64.b64decode(data["content"]).decode("utf-8")
                content_json = json.loads(content)
                if "objects" not in content_json or len(content_json["objects"]) == 0:
                    raise ValueError("JSON data type is not a STIX2 bundle")
                if len(content_json["objects"]) == 1:
                    update = data["update"] if "update" in data else False
                    imported_items = self.api.stix2.import_bundle_from_json(
                        content, update, types, work_id
                    )
                else:
                    # As bundle is received as complete, split and requeue
                    # Create a specific channel to push the split bundles
                    push_pika_connection = pika.BlockingConnection(self.pika_parameters)
                    push_channel = push_pika_connection.channel()
                    try:
                        push_channel.confirm_delivery()
                    except Exception as err:  # pylint: disable=broad-except
                        self.worker_logger.warning(str(err))
                    # Instance spliter and split the big bundle
                    event_version = (
                        content_json["x_opencti_event_version"]
                        if "x_opencti_event_version" in content_json
                        else None
                    )
                    stix2_splitter = OpenCTIStix2Splitter()
                    expectations, bundles = (
                        stix2_splitter.split_bundle_with_expectations(
                            content_json, False, event_version
                        )
                    )
                    # Add expectations to the work
                    if work_id is not None:
                        self.api.work.add_expectations(work_id, expectations)
                    # For each split bundle, send it to the same queue
                    for bundle in bundles:
                        text_bundle = json.dumps(bundle)
                        data["content"] = base64.b64encode(
                            text_bundle.encode("utf-8", "escape")
                        ).decode("utf-8")
                        push_channel.basic_publish(
                            exchange=self.connector["config"]["push_exchange"],
                            routing_key=self.connector["config"]["push_routing"],
                            body=json.dumps(data),
                            properties=pika.BasicProperties(
                                delivery_mode=2,
                                content_encoding="utf-8",  # make message persistent
                            ),
                        )
                    push_channel.close()
                    push_pika_connection.close()
            elif event_type == "event":
                event = base64.b64decode(data["content"]).decode("utf-8")
                event_content = json.loads(event)
                event_type = event_content["type"]
                if event_type == "create" or event_type == "update":
                    bundle = {
                        "type": "bundle",
                        "objects": [event_content["data"]],
                    }
                    imported_items = self.api.stix2.import_bundle(
                        bundle, True, types, work_id
                    )
                elif event_type == "delete":
                    delete_object = event_content["data"]
                    delete_object["opencti_operation"] = event_type
                    bundle = {
                        "type": "bundle",
                        "objects": [delete_object],
                    }
                    imported_items = self.api.stix2.import_bundle(
                        bundle, True, types, work_id
                    )
                elif event_type == "merge":
                    # Start with a merge
                    target_id = event_content["data"]["id"]
                    source_ids = list(
                        map(
                            lambda source: source["id"],
                            event_content["context"]["sources"],
                        )
                    )
                    merge_object = event_content["data"]
                    merge_object["opencti_operation"] = "merge"
                    merge_object["merge_target_id"] = target_id
                    merge_object["merge_source_ids"] = source_ids
                    bundle = {
                        "type": "bundle",
                        "objects": [merge_object],
                    }
                    imported_items = self.api.stix2.import_bundle(
                        bundle, True, types, work_id
                    )
                else:
                    raise ValueError(
                        "Unsupported operation type", {"event_type": event_type}
                    )
            else:
                raise ValueError("Unsupported event type", {"event_type": event_type})
        except Exception as ex:
            # Technical unmanaged exception
            self.worker_logger.error(
                "Error executing data handling", {"reason": str(ex)}
            )
        finally:
            bundles_global_counter.add(len(imported_items))
            processing_delta = datetime.datetime.now() - start_processing
            bundles_processing_time_gauge.record(processing_delta.seconds)
            # Ack the message
            cb = functools.partial(self.ack_message, channel, delivery_tag)
            connection.add_callback_threadsafe(cb)
            return True

    def run(self) -> None:
        try:
            # Consume the queue
            self.worker_logger.info(
                "Thread for queue started", {"queue": self.queue_name}
            )
            self.channel.basic_consume(
                queue=self.queue_name,
                on_message_callback=self._process_message,
            )
            self.channel.start_consuming()
        finally:
            self.channel.stop_consuming()
            self.worker_logger.info(
                "Thread for queue terminated", {"queue": self.queue_name}
            )


@dataclass(unsafe_hash=True)
class Worker:  # pylint: disable=too-few-public-methods, too-many-instance-attributes
    logs_all_queue: str = "logs_all"
    consumer_threads: Dict[str, Any] = field(default_factory=dict, hash=False)
    listen_api_threads: Dict[str, Any] = field(default_factory=dict, hash=False)
    logger_threads: Dict[str, Any] = field(default_factory=dict, hash=False)

    def __post_init__(self) -> None:
        # Get configuration
        config_file_path = os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "config.yml"
        )
        if os.path.isfile(config_file_path):
            with open(config_file_path, "r") as f:
                config = yaml.load(f, Loader=yaml.FullLoader)
        else:
            config = {}

        # Load API config
        self.config = config
        self.opencti_url = get_config_variable(
            "OPENCTI_URL", ["opencti", "url"], config
        )
        self.opencti_token = get_config_variable(
            "OPENCTI_TOKEN", ["opencti", "token"], config
        )
        self.opencti_ssl_verify = get_config_variable(
            "OPENCTI_SSL_VERIFY", ["opencti", "ssl_verify"], config, default=False
        )
        self.opencti_json_logging = get_config_variable(
            "OPENCTI_JSON_LOGGING", ["opencti", "json_logging"], config, default=True
        )
        self.opencti_pool_size = get_config_variable(
            "OPENCTI_EXECUTION_POOL_SIZE",
            ["opencti", "execution_pool_size"],
            config,
            default=5,
        )
        self.listen_pool_size = get_config_variable(
            "WORKER_LISTEN_POOL_SIZE",
            ["worker", "listen_pool_size"],
            config,
            default=5,
        )
        # Load worker config
        self.log_level = get_config_variable(
            "WORKER_LOG_LEVEL", ["worker", "log_level"], config
        )
        self.listen_api_ssl_verify = get_config_variable(
            "WORKER_LISTEN_API_SSL_VERIFY",
            ["worker", "listen_api_ssl_verify"],
            config,
            default=False,
        )
        self.listen_api_http_proxy = get_config_variable(
            "WORKER_LISTEN_API_HTTP_PROXY",
            ["worker", "listen_api_http_proxy"],
            config,
            default="",
        )
        self.listen_api_https_proxy = get_config_variable(
            "WORKER_LISTEN_API_HTTPS_PROXY",
            ["worker", "listen_api_https_proxy"],
            config,
            default="",
        )
        # Telemetry
        self.telemetry_enabled = get_config_variable(
            "WORKER_TELEMETRY_ENABLED",
            ["worker", "telemetry_enabled"],
            config,
            False,
            False,
        )
        self.telemetry_prometheus_port = get_config_variable(
            "WORKER_PROMETHEUS_TELEMETRY_PORT",
            ["worker", "telemetry_prometheus_port"],
            config,
            True,
            14270,
        )
        self.telemetry_prometheus_host = get_config_variable(
            "WORKER_PROMETHEUS_TELEMETRY_HOST",
            ["worker", "telemetry_prometheus_host"],
            config,
            False,
            "0.0.0.0",
        )

        # Telemetry
        if self.telemetry_enabled:
            start_http_server(
                port=self.telemetry_prometheus_port, addr=self.telemetry_prometheus_host
            )
            provider = MeterProvider(
                resource=resource, metric_readers=[PrometheusMetricReader()]
            )
            metrics.set_meter_provider(provider)

        # Check if openCTI is available
        self.api = OpenCTIApiClient(
            url=self.opencti_url,
            token=self.opencti_token,
            log_level=self.log_level,
            ssl_verify=self.opencti_ssl_verify,
            json_logging=self.opencti_json_logging,
        )
        self.worker_logger = self.api.logger_class("worker")
        # Initialize variables
        self.connectors: List[Any] = []
        self.queues: List[Any] = []
        self.execution_pool = ThreadPoolExecutor(max_workers=self.opencti_pool_size)
        self.listen_api_execution_pool = ThreadPoolExecutor(
            max_workers=self.listen_pool_size
        )

    # Start the main loop
    def start(self) -> None:
        sleep_delay = 60
        while True:
            try:
                # Fetch queue configuration from API
                self.queues = list()
                self.connectors = self.api.connector.list()
                # Check if all queues are consumed
                for connector in self.connectors:
                    # Push to ingest message
                    push_queue = connector["config"]["push"]
                    self.queues.append(push_queue)
                    if push_queue in self.consumer_threads:
                        if not self.consumer_threads[push_queue].is_alive():
                            self.worker_logger.info(
                                "Thread for queue not alive, creating a new one...",
                                {"queue": push_queue},
                            )
                            self.consumer_threads[push_queue] = Consumer(
                                self.execution_pool,
                                connector,
                                self.config,
                                self.opencti_url,
                                self.opencti_token,
                                self.log_level,
                                self.opencti_ssl_verify,
                                self.opencti_json_logging,
                            )
                            self.consumer_threads[push_queue].start()
                    else:
                        self.consumer_threads[push_queue] = Consumer(
                            self.execution_pool,
                            connector,
                            self.config,
                            self.opencti_url,
                            self.opencti_token,
                            self.log_level,
                            self.opencti_ssl_verify,
                            self.opencti_json_logging,
                        )
                        self.consumer_threads[push_queue].start()
                    # Listen for webhook message
                    if connector["config"].get("listen_callback_uri") is not None:
                        listen_queue = connector["config"]["listen"]
                        self.queues.append(listen_queue)
                        if listen_queue in self.listen_api_threads:
                            if not self.listen_api_threads[listen_queue].is_alive():
                                self.listen_api_threads[listen_queue] = ApiConsumer(
                                    self.listen_api_execution_pool,
                                    connector,
                                    self.config,
                                    self.opencti_token,
                                    self.listen_api_ssl_verify,
                                    self.listen_api_http_proxy,
                                    self.listen_api_https_proxy,
                                    self.log_level,
                                    self.opencti_json_logging,
                                )
                                self.listen_api_threads[listen_queue].start()
                        else:
                            self.listen_api_threads[listen_queue] = ApiConsumer(
                                self.listen_api_execution_pool,
                                connector,
                                self.config,
                                self.opencti_token,
                                self.listen_api_ssl_verify,
                                self.listen_api_http_proxy,
                                self.listen_api_https_proxy,
                                self.log_level,
                                self.opencti_json_logging,
                            )
                            self.listen_api_threads[listen_queue].start()

                # Check if some threads must be stopped
                for thread in list(self.consumer_threads):
                    if thread not in self.queues:
                        self.worker_logger.info(
                            "Queue no longer exists, killing thread...",
                            {"thread": thread},
                        )
                        try:
                            self.consumer_threads[thread].terminate()
                            self.consumer_threads.pop(thread, None)
                        except:
                            self.worker_logger.info(
                                "Unable to kill the thread for queue, an operation is running, keep trying...",
                                {"thread": thread},
                            )
                time.sleep(sleep_delay)
            except KeyboardInterrupt:
                # Graceful stop
                for thread in self.consumer_threads:
                    if thread not in self.queues:
                        self.consumer_threads[thread].terminate()
                sys.exit(0)
            except Exception as e:  # pylint: disable=broad-except
                self.worker_logger.error(type(e).__name__, {"reason": str(e)})
                time.sleep(60)


if __name__ == "__main__":
    worker = Worker()
    try:
        worker.start()
    except Exception as e:  # pylint: disable=broad-except
        sys.exit(1)

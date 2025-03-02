#     Copyright 2024. ThingsBoard
#
#     Licensed under the Apache License, Version 2.0 (the "License");
#     you may not use this file except in compliance with the License.
#     You may obtain a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#     Unless required by applicable law or agreed to in writing, software
#     distributed under the License is distributed on an "AS IS" BASIS,
#     WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#     See the License for the specific language governing permissions and
#     limitations under the License.

import asyncio
from random import choice
from re import search
from socket import gethostbyname
from string import ascii_lowercase
from threading import Thread
from time import sleep, time

from thingsboard_gateway.connectors.connector import Connector
from thingsboard_gateway.tb_utility.tb_loader import TBModuleLoader
from thingsboard_gateway.tb_utility.tb_utility import TBUtility
from thingsboard_gateway.tb_utility.tb_logger import init_logger

# Try import library or install it and import
installation_required = False

try:
    from puresnmp import __version__ as pymodbus_version

    if int(pymodbus_version.split('.')[0]) < 2:
        installation_required = True
except ImportError:
    installation_required = True

if installation_required:
    print("Puresnmp library not found - installing...")
    TBUtility.install_package("puresnmp", ">=2.0.0")

# <hb> added import for version of the snmp module
from puresnmp import Client, credentials, PyWrapper, V2C
from puresnmp.exc import Timeout as SNMPTimeoutException


class SNMPConnector(Connector, Thread):
    def __init__(self, gateway, config, connector_type):
        super().__init__()
        self.daemon = True
        self.__gateway = gateway
        self._connected = False
        self.__stopped = False
        self._connector_type = connector_type
        self.__config = config
        self.__id = self.__config.get('id')
        self.name = config.get("name", 'SNMP Connector ' + ''.join(choice(ascii_lowercase) for _ in range(5)))
        self._log = init_logger(self.__gateway, self.name, self.__config.get('logLevel', 'INFO'))
        self.__devices = self.__config["devices"]
        self.statistics = {'MessagesReceived': 0,
                           'MessagesSent': 0}
        self._default_converters = {
            "uplink": "SNMPUplinkConverter",
            "downlink": "SNMPDownlinkConverter"
        }
        self.__methods = ["get", "multiget", "getnext", "walk", "multiwalk", "set", "multiset",
                          "bulkget", "bulkwalk", "table", "bulktable"]
        self.__datatypes = ('attributes', 'telemetry')

        self.__loop = asyncio.new_event_loop()
        self.__short_interval_mode = False


    def open(self):
        self.__stopped = False
        self.__fill_converters()
        self.start()

    def run(self):
        self._connected = True
        try:
            self.__loop.run_until_complete(self._run())
        except Exception as e:
            self._log.exception(e)

    async def _run(self):
        while not self.__stopped:
            current_time = time() * 1000
            for device in self.__devices:
                try:
                    if device.get("shortIntervalPoll", "false") == "true" and self.__short_interval_mode:
                        poll_interval = 10000
                    else:
                        poll_interval = device.get("pollPeriod", 10000)
                    if device.get("previous_poll_time", 0) + poll_interval < current_time:
                        await self.__process_data(device)
                        device["previous_poll_time"] = current_time
                except Exception as e:
                    self._log.exception(e)
            if self.__stopped:
                break
            else:
                sleep(.2)

    def close(self):
        self.__stopped = True
        self._connected = False

    def get_id(self):
        return self.__id

    def get_name(self):
        return self.name

    def get_type(self):
        return self._connector_type

    def is_connected(self):
        return self._connected

    def is_stopped(self):
        return self.__stopped

    def get_config(self):
        return self.__config

    def collect_statistic_and_send(self, connector_name, connector_id, data):
        self.statistics["MessagesReceived"] = self.statistics["MessagesReceived"] + 1
        self.__gateway.send_to_storage(connector_name, connector_id, data)
        self.statistics["MessagesSent"] = self.statistics["MessagesSent"] + 1

    async def __process_data(self, device):
        common_parameters = self.__get_common_parameters(device)
        # hb
        # converted_data = {}
        converted_data = {
            "deviceName": device["deviceName"],
            "deviceType": device["deviceType"],
            "attributes": [],
            "telemetry": []
            }
        # /hb

        for datatype in self.__datatypes:               # i.e. telemetry or attributes
            for datatype_config in device[datatype]:
                try:
                    response = None
                    method = datatype_config.get("method")
                    if method is None:
                        self._log.error("Method not found in configuration: %r", datatype_config)
                        continue
                    else:
                        method = method.lower()
                    if method not in self.__methods:
                        self._log.error("Unknown method: %s, configuration is: %r", method, datatype_config)
                    response = await self.__process_methods(method, common_parameters, datatype_config)
                    # print("converted data before: ", converted_data)
                    # print("device data type", datatype)
                    # converted_data.update(**device["uplink_converter"].convert((datatype, datatype_config), response))

                    # <hb> check if there are alarms active requiring short interval polling
                    if (datatype_config["key"] == "upsAlarmsPresent") and response > 0:
                        self.__short_interval_mode = True
                    else:
                        self.__short_interval_mode = False

                    converted_data[datatype].append(device["uplink_converter"].convert((datatype, datatype_config), response))
                    # print("<hb> converted data after: ", converted_data)
                except SNMPTimeoutException:
                    self._log.error("Timeout exception on connection to device \"%s\" with ip: \"%s\"", device["deviceName"],
                              device["ip"])
                    return
                except Exception as e:
                    self._log.exception(e)

        # print("<hb> converted data ", converted_data)
        if isinstance(converted_data, dict) and (converted_data.get("attributes") or converted_data.get("telemetry")):
            self.collect_statistic_and_send(self.get_name(), self.get_id(), converted_data)

    async def __process_methods(self, method, common_parameters, datatype_config):
        # changes for snmp version 2
        #client = Client(ip=common_parameters['ip'],
        #                port=common_parameters['port'],
        #                credentials=credentials.V1(common_parameters['community']))
        client = Client(ip=common_parameters['ip'],
                        port=common_parameters['port'],
                        credentials=V2C(common_parameters['community']))

        client.configure(timeout=common_parameters['timeout'])
        client = PyWrapper(client)
        #print('>>', method)
        response = None

        if method == "get":
            oid = datatype_config["oid"]
            response = await client.get(oid=oid)

        elif method == "multiget":
            oids = datatype_config["oid"]
            oids = oids if isinstance(oids, list) else list(oids)
            response = await client.multiget(oids=oids)
        elif method == "getnext":
            oid = datatype_config["oid"]
            master_response = await client.getnext(oid=oid)
            response = {master_response.oid: master_response.value}
        elif method == "walk":
            oid = datatype_config["oid"]
            response = {}
            async for binded_var in client.walk(oid=oid):
                response[binded_var.oid] = binded_var.value
        elif method == "multiwalk":
            oids = datatype_config["oid"]
            oids = oids if isinstance(oids, list) else list(oids)
            response = {}
            async for binded_var in client.multiwalk(oids=oids):
                response[binded_var.oid] = binded_var.value
        elif method == "set":
            oid = datatype_config["oid"]
            value = datatype_config["value"]
            response = await client.set(oid=oid, value=value)
        elif method == "multiset":
            mappings = datatype_config["mappings"]
            response = await client.multiset(mappings=mappings)
        elif method == "bulkget":
            scalar_oids = datatype_config.get("scalarOid", [])
            scalar_oids = scalar_oids if isinstance(scalar_oids, list) else list(scalar_oids)
            repeating_oids = datatype_config.get("repeatingOid", [])
            repeating_oids = repeating_oids if isinstance(repeating_oids, list) else list(repeating_oids)
            max_list_size = datatype_config.get("maxListSize", 1)
            response = await client.bulkget(scalar_oids=scalar_oids, repeating_oids=repeating_oids,
                                            max_list_size=max_list_size)
            response = response.scalars
        elif method == "bulkwalk":
            oids = datatype_config["oid"]
            oids = oids if isinstance(oids, list) else list(oids)
            bulk_size = datatype_config.get("bulkSize", 10)
            response = {}
            async for binded_var in client.bulkwalk(bulk_size=bulk_size, oids=oids):
                response[binded_var.oid] = binded_var.value
        elif method == "table":
            oid = datatype_config["oid"]
            num_base_nodes = datatype_config.get("numBaseNodes", 0)
            response = await client.table(oid=oid)
        elif method == "bulktable":
            oid = datatype_config["oid"]
            num_base_nodes = datatype_config.get("numBaseNodes", 0)
            bulk_size = datatype_config.get("bulkSize", 10)
            response = await client.bulktable(oid=oid, bulk_size=bulk_size)
        else:
            self._log.error("Method \"%s\" - Not found", str(method))
        return response

    def __fill_converters(self):
        try:
            for device in self.__devices:
                device["uplink_converter"] = TBModuleLoader.import_module("snmp", device.get('converter',
                                                                                             self._default_converters[
                                                                                                 "uplink"]))(device,
                                                                                                             self._log)
                device["downlink_converter"] = TBModuleLoader.import_module("snmp", device.get('converter',
                                                                                               self._default_converters[
                                                                                                   "downlink"]))(device)
        except Exception as e:
            self._log.exception(e)

    @staticmethod
    def __get_common_parameters(device):
        return {"ip": gethostbyname(device["ip"]),
                "port": device.get("port", 161),
                "timeout": device.get("timeout", 6),
                "community": device["community"],
                }

    def on_attributes_update(self, content):
        try:
            for device in self.__devices:
                if content["device"] == device["deviceName"]:
                    for attribute_request_config in device["attributeUpdateRequests"]:
                        for attribute, value in content["data"]:
                            if search(attribute, attribute_request_config["attributeFilter"]):
                                common_parameters = self.__get_common_parameters(device)
                                result = self.__process_methods(attribute_request_config["method"], common_parameters,
                                                                {**attribute_request_config, "value": value})
                                self._log.debug(
                                    "Received attribute update request for device \"%s\" "
                                    "with attribute \"%s\" and value \"%s\"",
                                    content["device"],
                                    attribute)
                                self._log.debug(result)
                                self._log.debug(content)
        except Exception as e:
            self._log.exception(e)

    def server_side_rpc_handler(self, content):
        try:
            for device in self.__devices:
                if content["device"] == device["deviceName"]:
                    if(device.get("serverSideRpcRequests") is not None):
                        for rpc_request_config in device["serverSideRpcRequests"]:
                            if search(content["data"]["method"], rpc_request_config["requestFilter"]):
                                common_parameters = self.__get_common_parameters(device)
                                result = self.__process_methods(rpc_request_config["method"], common_parameters,
                                                                {**rpc_request_config, "value": content["data"]["params"]})
                                print('>>>RPC for',content["device"])
                                self._log.debug("Received RPC request for device \"%s\" with command \"%s\" and value \"%s\"",
                                          content["device"],
                                          content["data"]["method"])
                                self._log.debug(result)
                                self._log.debug(content)
                                # hb - self.__gateway.send_rpc_reply(device=content["device"], req_id=content["data"]["id"],
                                # hb -                              content=result)
        except Exception as e:
            self._log.exception(e)
            self.__gateway.send_rpc_reply(device=content["device"], req_id=content["data"]["id"], success_sent=False)

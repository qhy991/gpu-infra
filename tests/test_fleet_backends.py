import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from kernel_infra.contracts import ContractError
from kernel_infra.fleet import load_fleet_catalog, probe_node, select_node, remote_kernelctl_json, FleetSelectionError
from test_fleet import node_status


class HeterogeneousFleetTests(unittest.TestCase):
    def catalog(self, directory):
        path = Path(directory)/'catalog.json'
        path.write_text(json.dumps({'schema':'kernelinfra.fleet.v2','nodes':[{'id':'mac','ssh':'localhost','transport':'local','kernelctl':'/usr/bin/kernelctl','socket':'/tmp/test.sock','inbox':'/tmp/inbox','capabilities':['metal']}]}))
        return load_fleet_catalog(path)

    def test_local_probe_and_query_do_not_use_ssh_or_shell(self):
        with tempfile.TemporaryDirectory() as directory:
            catalog = self.catalog(directory)
            def run(argv, **kwargs):
                self.assertEqual(argv[0], '/usr/bin/kernelctl')
                self.assertFalse(kwargs.get('shell', False))
                return subprocess.CompletedProcess(argv, 0, json.dumps(node_status()), '')
            self.assertEqual(probe_node(catalog.nodes[0], catalog, run=run)['status'], 'ok')
            with patch('kernel_infra.fleet.subprocess.run', side_effect=run):
                remote_kernelctl_json(node=catalog.nodes[0], catalog=catalog, arguments=['status'])

    def test_catalog_label_cannot_fabricate_backend(self):
        with tempfile.TemporaryDirectory() as directory:
            catalog = self.catalog(directory)
            status = node_status()
            observation = {'node_id':'mac','status':'ok','node':status}
            kwargs = dict(catalog=catalog, observations=[observation], required_capabilities={'metal'}, required_deployments=set(), min_free_bytes=0)
            with self.assertRaises(FleetSelectionError):
                select_node(**kwargs)
            status['broker']['backend'] = 'metal'
            self.assertEqual(select_node(**kwargs)[0].node_id, 'mac')
            status['broker']['probe_error'] = 'unknown occupancy'
            with self.assertRaises(FleetSelectionError):
                select_node(**kwargs)

    def test_v1_cannot_silently_enable_local_execution(self):
        with tempfile.TemporaryDirectory() as directory:
            catalog = self.catalog(directory)
            value = json.loads(catalog.source_path.read_text())
            value['schema'] = 'kernelinfra.fleet.v1'
            catalog.source_path.write_text(json.dumps(value))
            with self.assertRaises(ContractError):
                load_fleet_catalog(catalog.source_path)

    def test_amd_label_requires_amd_broker_not_hygon(self):
        with tempfile.TemporaryDirectory() as directory:
            catalog = self.catalog(directory)
            value = json.loads(catalog.source_path.read_text())
            value['nodes'][0]['capabilities'] = ['amd']
            catalog.source_path.write_text(json.dumps(value))
            catalog = load_fleet_catalog(catalog.source_path)
            status = node_status()
            kwargs = dict(catalog=catalog,
                observations=[{'node_id':'mac', 'status':'ok', 'node':status}],
                required_capabilities={'amd'}, required_deployments=set(), min_free_bytes=0)
            for backend in ('nvidia', 'hygon', None):
                status['broker']['backend'] = backend
                with self.assertRaises(FleetSelectionError):
                    select_node(**kwargs)
            status['broker']['backend'] = 'amd'
            self.assertEqual(select_node(**kwargs)[0].node_id, 'mac')

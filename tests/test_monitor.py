import asyncio
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
from defusedxml.common import EntitiesForbidden
from fastapi.testclient import TestClient
from pydantic import ValidationError

from gpu_monitor import collector, main
from gpu_monitor.main import Monitor, Server, Settings

XML = '''<nvidia_smi_log><driver_version>580.126.20</driver_version><cuda_version>13.0</cuda_version>
<gpu id="00000000:21:00.0"><product_name>NVIDIA RTX PRO 5000</product_name><uuid>GPU-test</uuid><minor_number>7</minor_number>
<fb_memory_usage><total>73415 MiB</total><used>44354 MiB</used><free>28540 MiB</free></fb_memory_usage>
<utilization><gpu_util>0 %</gpu_util></utilization><temperature><gpu_temp>63 C</gpu_temp></temperature>
<gpu_power_readings><instant_power_draw>194.5 W</instant_power_draw><current_power_limit>350 W</current_power_limit></gpu_power_readings>
<processes><process_info><pid>123</pid><type>C</type><process_name>python</process_name><used_memory>8936 MiB</used_memory></process_info>
<process_info><pid>456</pid><type>G</type><process_name>renderer</process_name><used_memory>697 MiB</used_memory></process_info></processes></gpu></nvidia_smi_log>'''


def test_modern_xml_preserves_free_memory_and_smi_index():
    gpu = collector.parse_xml(XML)['gpus'][0]
    assert gpu['index'] == 0  # SMI index, not Linux minor_number=7
    assert gpu['memory_free_mib'] == 28540  # respects driver reservation
    assert gpu['power_w'] == 194.5
    assert gpu['power_limit_w'] == 350
    assert [p['type'] for p in gpu['processes']] == ['C', 'G']


def test_unavailable_metrics_are_unknown():
    gpu = collector.parse_xml(XML.replace('<gpu_util>0 %', '<gpu_util>N/A').replace('<gpu_temp>63 C', '<gpu_temp>N/A'))['gpus'][0]
    assert gpu['utilization'] is None
    assert gpu['temperature'] is None
    assert collector.number('NaN') is None


def test_rejects_xml_entities():
    with pytest.raises(EntitiesForbidden):
        collector.parse_xml('<!DOCTYPE x [<!ENTITY a "expanded">]><nvidia_smi_log>&a;</nvidia_smi_log>')


def test_fictional_eight_card_snapshot():
    data = collector.parse_snapshot((main.ROOT / 'examples/nvidia-smi.txt').read_text())
    assert [g['index'] for g in data['gpus']] == list(range(8))
    assert data['gpus'][0]['memory_used_mib'] == 32768
    assert len(data['gpus'][0]['processes']) == 2
    assert data['gpus'][4]['memory_free_mib'] == 81904


def test_owner_join_keeps_only_gpu_pids():
    data = collector.attach_owners(collector.parse_xml(XML), ' 123 alice python train.py --name a b\n999 other secret unrelated\n')
    processes = data['gpus'][0]['processes']
    assert processes[0]['user'] == 'alice'
    assert processes[0]['command'] == 'python train.py --name a b'
    assert processes[1]['user'] is None
    assert len(processes) == 2
    assert 'unrelated' not in str(data)


def test_ssh_is_bounded_noninteractive_and_readonly(monkeypatch):
    calls = []
    def run(command, **kwargs):
        calls.append((command, kwargs))
        return SimpleNamespace(returncode=0, stdout=XML, stderr='')
    monkeypatch.setattr(collector.shutil, 'which', lambda _: '/usr/bin/ssh')
    monkeypatch.setattr(collector.subprocess, 'run', run)
    collector.collect(Server(name='Company', target='gpu-alias'))
    command, options = calls[0]
    assert command[-2:] == ['gpu-alias', 'nvidia-smi -q -x']
    assert 'StrictHostKeyChecking=yes' in command
    assert 'BatchMode=yes' in command
    assert options['timeout'] == 18
    assert not options.get('shell')


def test_ssh_timeout_is_clear(monkeypatch):
    monkeypatch.setattr(collector.shutil, 'which', lambda _: '/usr/bin/ssh')
    def timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired('ssh', 18)
    monkeypatch.setattr(collector.subprocess, 'run', timeout)
    with pytest.raises(RuntimeError, match='超时'):
        collector.collect(Server(name='Company', target='gpu-alias'))


@pytest.mark.parametrize('target', ['-oProxyCommand=bad', 'host;whoami', 'host bad', '-F@host'])
def test_rejects_ssh_option_injection(target):
    with pytest.raises(ValidationError):
        Server(name='Company', target=target)


def test_connection_failure_retains_but_marks_old_data(tmp_path, monkeypatch):
    monitor = Monitor(tmp_path / 'config.json')
    server = Server(name='Test', target='test-alias')
    monitor.save(Settings(servers=[server]))
    monkeypatch.setattr(main, 'collect', lambda _: collector.parse_xml(XML))
    asyncio.run(monitor.poll())
    assert monitor.snapshot()['servers'][0]['gpus'][0]['status'] == 'zero'
    assert monitor.histories
    def fail(_):
        raise RuntimeError('SSH unavailable')
    monkeypatch.setattr(main, 'collect', fail)
    asyncio.run(monitor.poll())
    result = monitor.snapshot()
    assert not result['demo']
    assert result['servers'][0]['error'] == 'SSH unavailable'
    assert result['servers'][0]['gpus'][0]['status'] == 'offline'
    assert result['servers'][0]['gpus'][0]['history'] == []
    monitor.save(Settings(servers=[server.model_copy(update={'target': 'new-host'})]))
    assert not monitor.states  # never retain old host's readings after an edit


def test_api_settings_detail_and_cross_site_guard(tmp_path, monkeypatch):
    monitor = Monitor(tmp_path / 'config.json')
    monkeypatch.setattr(main, 'Monitor', lambda: monitor)
    def collect(server, with_owners=False):
        result = collector.parse_xml(XML)
        return collector.attach_owners(result, '123 alice python train.py') if with_owners else result
    monkeypatch.setattr(main, 'collect', collect)
    with TestClient(main.app) as client:
        assert client.get('/').status_code == 200
        assert client.get('/api/state').json()['demo']
        settings = {'interval_seconds': 5, 'servers': [{'id':'test','name':'Test','target':'gpu-alias'}]}
        assert client.put('/api/settings', json=settings, headers={'Origin':'https://evil.example'}).status_code == 403
        assert not monitor.path.exists()
        assert client.put('/api/settings', json=settings).status_code == 200
        assert Settings.model_validate_json(monitor.path.read_text()).servers[0].target == 'gpu-alias'
        result = client.get('/api/servers/test/gpus/GPU-test/processes')
        assert result.status_code == 200
        assert result.json()['processes'][0]['user'] == 'alice'
        assert client.get('/api/servers/missing/gpus/GPU-test/processes').status_code == 404
        assert client.put('/api/settings', json={**settings,'interval_seconds':0}).status_code == 422
        assert client.get('/', headers={'Host':'evil.example'}).status_code == 400

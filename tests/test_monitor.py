import asyncio
import subprocess
from collections import deque
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
    updated_at = monitor.snapshot()['servers'][0]['updated_at']
    assert updated_at is not None
    def fail(_):
        raise RuntimeError('SSH unavailable')
    monkeypatch.setattr(main, 'collect', fail)
    asyncio.run(monitor.poll())
    result = monitor.snapshot()
    assert not result['demo']
    assert result['servers'][0]['error'] == 'SSH unavailable'
    assert result['servers'][0]['gpus'][0]['status'] == 'offline'
    assert result['servers'][0]['updated_at'] == updated_at
    assert result['servers'][0]['gpus'][0]['memory_free_mib'] == 28540
    assert len(result['servers'][0]['gpus'][0]['history']) == 1
    monkeypatch.setattr(main, 'collect', lambda _: collector.parse_xml(XML))
    asyncio.run(monitor.poll())
    history = monitor.snapshot()['servers'][0]['gpus'][0]['history']
    assert len(history) == 2
    assert history[0]['gap_before'] is False
    assert history[1]['gap_before'] is True
    monitor.save(Settings(servers=[server.model_copy(update={'target': 'new-host'})]))
    assert not monitor.states  # never retain old host's readings after an edit
    assert not monitor.histories


def test_history_keeps_raw_samples_and_compresses_display(tmp_path, monkeypatch):
    monitor = Monitor(tmp_path / 'config.json')
    monitor.save(Settings(servers=[Server(name='Test', target='test-alias')]))
    now = [1000.0]
    monkeypatch.setattr(main.time, 'time', lambda: now[0])
    def collect(_):
        data = collector.parse_xml(XML)
        data['gpus'][0]['utilization'] = None if now[0] == 1100 else 75
        return data
    monkeypatch.setattr(main, 'collect', collect)
    async def sample():
        for _ in range(720):
            await monitor.poll()
            now[0] += 5
    asyncio.run(sample())
    state = monitor.snapshot()
    history = state['servers'][0]['gpus'][0]['history']
    gpu = state['servers'][0]['gpus'][0]
    assert len(history) == state['history_limit'] == 120
    assert state['history_window_seconds'] == 12 * 60 * 60
    assert gpu['history_count'] == 720
    assert gpu['history_group_size'] == 6
    assert gpu['history_start'] == 1000
    raw = monitor.histories[(monitor.settings.servers[0].id, 'GPU-test')]
    assert len(raw) == 720
    assert next(point for point in raw if point['at'] == 1100)['utilization'] is None
    assert any(point['gap_before'] for point in history)
    assert history[-1]['utilization'] == 75
    assert len(gpu['recent_history']) == 60
    assert gpu['recent_history'] == list(raw)[-60:]


def history_samples(values, start=1000, interval=5):
    return [{'at': start + i * interval, 'utilization': value,
             'interval_seconds': interval, 'gap_before': False}
            for i, value in enumerate(values)]


@pytest.mark.parametrize('interval', [5, 60])
def test_history_rolls_over_twelve_hours_even_without_new_samples(tmp_path, monkeypatch, interval):
    monitor = Monitor(tmp_path / 'config.json')
    server = Server(name='Test', target='test-alias')
    monitor.save(Settings(interval_seconds=interval, servers=[server]))
    now = [1000 + 13 * 60 * 60]
    monkeypatch.setattr(main.time, 'time', lambda: now[0])
    monkeypatch.setattr(main, 'collect', lambda _: collector.parse_xml(XML))
    key = (server.id, 'GPU-test')
    monitor.histories[key] = deque(history_samples([75] * (13 * 60 * 60 // interval), interval=interval))
    asyncio.run(monitor.poll())
    gpu = monitor.snapshot()['servers'][0]['gpus'][0]
    assert gpu['history_start'] == now[0] - main.HISTORY_WINDOW_SECONDS
    assert gpu['history_count'] == main.HISTORY_WINDOW_SECONDS // interval + 1
    assert len(gpu['history']) <= 120
    assert all(point['at'] >= now[0] - main.HISTORY_WINDOW_SECONDS for point in gpu['history'])
    # Prune on reads too: a disconnected server must not keep showing old data.
    now[0] += main.HISTORY_WINDOW_SECONDS + 1
    gpu = monitor.snapshot()['servers'][0]['gpus'][0]
    assert gpu['history_count'] == 0
    assert gpu['history'] == gpu['recent_history'] == []
    assert not monitor.histories[key]


@pytest.mark.parametrize('count', [0, 1, 119, 120, 121, 719, 720, 721, 8641])
def test_compression_is_bounded_and_preserves_each_group_peak(count):
    samples = history_samples([(i * 17) % 101 for i in range(count)])
    original = [dict(point) for point in samples]
    compressed, group_size = main.compress_history(samples)
    assert len(compressed) <= 120
    assert group_size == max(1, (count + 119) // 120)
    for i, point in enumerate(compressed):
        group = samples[i * group_size:(i + 1) * group_size]
        peak = max(group, key=lambda p: (p['utilization'], p['at']))
        assert point == peak
    assert samples == original  # repeated compression always uses raw data


def test_compression_preserves_gaps_hidden_inside_groups():
    samples = history_samples([10] * 720)
    samples[0]['utilization'] = 99
    samples[3]['gap_before'] = True  # after the first group's peak
    samples[6]['utilization'] = 90
    samples[8]['utilization'] = None  # after the second group's peak
    samples[12]['utilization'] = 80
    samples[18]['gap_before'] = True  # before the fourth group's peak
    for point in samples[24:30]:
        point['utilization'] = None
    compressed, group_size = main.compress_history(samples)
    assert group_size == 6
    assert [p['gap_before'] for p in compressed[:4]] == [False, True, True, True]
    assert compressed[4]['utilization'] is None
    assert compressed[5]['gap_before'] is True


def test_history_marks_long_gaps_but_keeps_interval_changes_continuous(tmp_path, monkeypatch):
    monitor = Monitor(tmp_path / 'config.json')
    server = Server(name='Test', target='test-alias')
    monitor.save(Settings(interval_seconds=60, servers=[server]))
    now = [1000.0]
    monkeypatch.setattr(main.time, 'time', lambda: now[0])
    monkeypatch.setattr(main, 'collect', lambda _: collector.parse_xml(XML))
    asyncio.run(monitor.poll())
    monitor.save(Settings(interval_seconds=5, servers=[server]))
    now[0] = 1062
    asyncio.run(monitor.poll())
    now[0] = 1200
    asyncio.run(monitor.poll())
    history = monitor.snapshot()['servers'][0]['gpus'][0]['history']
    assert [point['gap_before'] for point in history] == [False, False, True]


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

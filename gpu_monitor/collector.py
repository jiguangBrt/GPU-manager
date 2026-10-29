"""Read-only SSH telemetry; process ownership is queried only on request."""

import math
import re
import shutil
import subprocess
from pathlib import Path

from defusedxml import ElementTree


def number(value):
    try:
        result = float(str(value).split()[0].rstrip('%'))
        return result if math.isfinite(result) else None
    except (ValueError, TypeError, IndexError):
        return None


def parse_xml(raw: str):
    root = ElementTree.fromstring(raw)
    gpus = []
    for index, node in enumerate(root.findall('gpu')):
        def read(path):
            return node.findtext(path)

        processes = []
        for process in node.findall('processes/process_info'):
            processes.append({
                'pid': process.findtext('pid'),
                'name': process.findtext('process_name') or '未知进程',
                'type': process.findtext('type') or '—',
                'memory_mib': number(process.findtext('used_memory')),
            })
        total = number(read('fb_memory_usage/total'))
        used = number(read('fb_memory_usage/used'))
        free = number(read('fb_memory_usage/free'))
        gpus.append({
            # Linux device minor numbers can be reversed; XML order matches SMI indices.
            'index': index,
            'uuid': read('uuid') or node.get('id') or str(index),
            'name': read('product_name') or 'NVIDIA GPU',
            'memory_total_mib': total, 'memory_used_mib': used,
            'memory_free_mib': free if free is not None else (max(0, total - used) if total is not None and used is not None else None),
            'utilization': number(read('utilization/gpu_util')),
            'temperature': number(read('temperature/gpu_temp')),
            'power_w': number(read('gpu_power_readings/instant_power_draw') or read('gpu_power_readings/power_draw') or read('power_readings/power_draw') or read('gpu_power_readings/average_power_draw')),
            'power_limit_w': number(read('gpu_power_readings/current_power_limit') or read('power_readings/power_limit')),
            'processes': processes,
        })
    if not gpus:
        raise ValueError('服务器没有返回 GPU 数据。请在远端确认 nvidia-smi -q -x 可以执行。')
    return {'driver': root.findtext('driver_version'), 'cuda': root.findtext('cuda_version'), 'gpus': gpus}


def parse_snapshot(raw: str):
    """Parse an nvidia-smi table, including the explicitly fictional demo fixture."""
    lines = raw.splitlines()
    gpus = []
    for i, line in enumerate(lines):
        match = re.match(r'\|\s+(\d+)\s+(NVIDIA .+?)\s+(?:On|Off)\s*\|', line)
        if not match or i + 1 >= len(lines):
            continue
        metrics = re.search(r'(\d+)C.*?(\d+)W\s*/\s*(\d+)W.*?\|\s*(\d+)MiB\s*/\s*(\d+)MiB\s*\|\s*(\d+)%', lines[i + 1])
        if not metrics:
            continue
        temp, power, limit, used, total, util = map(int, metrics.groups())
        index = int(match[1])
        gpus.append({'index': index, 'uuid': f'snapshot-{index}', 'name': match[2].strip(),
                     'memory_total_mib': total, 'memory_used_mib': used, 'memory_free_mib': total - used,
                     'utilization': util, 'temperature': temp, 'power_w': power, 'power_limit_w': limit, 'processes': []})
    by_index = {g['index']: g for g in gpus}
    for line in lines:
        match = re.match(r'\|\s+(\d+)\s+N/A\s+N/A\s+(\d+)\s+(\S+)\s+(.+?)\s+(\d+)MiB\s*\|', line)
        if match and int(match[1]) in by_index:
            by_index[int(match[1])]['processes'].append({'pid': match[2], 'type': match[3], 'name': match[4], 'memory_mib': int(match[5])})
    return {'driver': '580.126.20', 'cuda': '13.0', 'gpus': gpus}


PROCESS_SEPARATOR = '\n__GPU_MANAGER_PROCESSES__\n'


def attach_owners(data, raw):
    # Discard all non-GPU process rows. Do not log or persist the raw ps output.
    processes = {p['pid']: p for gpu in data['gpus'] for p in gpu['processes']}
    owners = {}
    for line in raw.splitlines():
        parts = line.strip().split(None, 2)
        if len(parts) == 3 and parts[0] in processes:
            owners[parts[0]] = {'user': parts[1], 'command': parts[2]}
    for gpu in data['gpus']:
        for process in gpu['processes']:
            process.update(owners.get(process['pid'], {'user': None, 'command': None}))
    return data


def collect(server, with_owners=False):
    ssh = shutil.which('ssh')
    if not ssh:
        raise RuntimeError('未找到 OpenSSH 客户端。Linux 请安装 openssh-client；Windows 请启用“OpenSSH 客户端”可选功能。')
    command = [ssh, '-T', '-o', 'BatchMode=yes', '-o', 'StrictHostKeyChecking=yes',
               '-o', 'ConnectTimeout=8', '-o', 'ServerAliveInterval=5', '-o', 'ServerAliveCountMax=1']
    if server.port:
        command.extend(['-p', str(server.port)])
    if server.identity_file:
        command.extend(['-i', str(Path(server.identity_file).expanduser())])
    remote_command = 'nvidia-smi -q -x'
    if with_owners:
        remote_command += " && printf '\\n__GPU_MANAGER_PROCESSES__\\n' && (ps -eo pid=,user:64=,args= 2>/dev/null || true)"
    command.extend([server.target, remote_command])
    try:
        result = subprocess.run(command, capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=18)
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError('SSH 采集超时，请检查 VPN、服务器地址或跳板机连接。') from exc
    if result.returncode:
        detail = result.stderr.strip()[-1200:]
        raise RuntimeError(f'SSH 采集失败：{detail or "请检查连接配置"}')
    xml, separator, process_output = result.stdout.partition(PROCESS_SEPARATOR)
    data = parse_xml(xml)
    return attach_owners(data, process_output) if with_owners else data

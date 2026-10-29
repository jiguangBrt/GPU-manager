"""Linux and Windows: uv run python run.py"""
import argparse

import uvicorn

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='GPU-manager 本机监控')
    parser.add_argument('--port', type=int, default=8766)
    args = parser.parse_args()
    uvicorn.run('gpu_monitor.main:app', host='127.0.0.1', port=args.port)

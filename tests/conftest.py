"""pytest 全局 fixture。用 unittest 运行时本文件不生效，各测试类自行调用 bootstrap_test_env。"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests._env import bootstrap_test_env  # noqa: E402

bootstrap_test_env()


def pytest_configure(config):
    config.addinivalue_line("markers", "slow: 需要真实服务或 SSH 的测试")

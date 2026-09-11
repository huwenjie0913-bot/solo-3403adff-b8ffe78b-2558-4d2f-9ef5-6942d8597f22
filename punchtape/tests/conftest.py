"""pytest 配置：隔离的临时数据库与扫描段目录。"""
import os
import tempfile

_tmp = tempfile.mkdtemp(prefix="punchtape_test_")
os.environ["PUNCHTAPE_DB"] = f"sqlite:///{_tmp}/test.db"
os.environ["PUNCHTAPE_SEGMENT_DIR"] = os.path.join(_tmp, "segments")

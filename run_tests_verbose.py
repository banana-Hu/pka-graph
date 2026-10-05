"""临时测试运行器:带进度输出与看门狗,避免 discover 静默卡死。(阶段收尾后可删)"""
import faulthandler
import sys
import unittest

faulthandler.dump_traceback_later(60, exit=True)

loader = unittest.TestLoader()
suite = loader.discover("tests")
runner = unittest.TextTestRunner(verbosity=2)
result = runner.run(suite)
sys.exit(0 if result.wasSuccessful() else 1)

import os
import sys

# 让测试直接 import pyreader（不依赖安装），且不需要 Qt：textio / cache 都是纯逻辑模块
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

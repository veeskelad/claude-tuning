import shutil
import tempfile

d = tempfile.mkdtemp()
shutil.rmtree(d)

import shutil
import os
import vitis

HERE = os.path.dirname(os.path.abspath(__file__))
WS = os.path.join(HERE, "ws")
SRC = os.path.normpath(os.path.join(HERE, "..", "helloworld.c"))

shutil.copyfile(SRC, os.path.join(WS, "snn_app", "src", "helloworld.c"))

client = vitis.create_client()
client.set_workspace(path=WS)
app = client.get_component(name="snn_app")
app.build()
print("BUILD CALL DONE")
vitis.dispose()

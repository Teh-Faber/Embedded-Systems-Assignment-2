import shutil, os, glob
import vitis

HERE = os.path.dirname(os.path.abspath(__file__))
WS = os.path.join(HERE, "ws")
SRC = os.path.normpath(os.path.join(HERE, "..", "helloworld.c"))
XPFM = os.path.join(WS, "snn_platform", "export", "snn_platform", "snn_platform.xpfm")

client = vitis.create_client()
client.set_workspace(path=WS)

app = client.create_app_component(
    name="snn_app",
    platform=XPFM,
    domain="standalone_ps7_cortexa9_0",
    template="hello_world",
)

dst = glob.glob(os.path.join(WS, "snn_app", "src", "helloworld.c"))[0]
shutil.copyfile(SRC, dst)
print("Replaced", dst)

app.build()
print("APP BUILT")
vitis.dispose()

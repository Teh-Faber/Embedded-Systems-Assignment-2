import shutil
import vitis

WS = r"C:\Users\Bluex\Desktop\FPGA\lab_8_SNN\vitis_ws\ws"
SRC = r"C:\Users\Bluex\Desktop\FPGA\lab_8_SNN\SNN_guest_lecture\code\helloworld.c"

shutil.copyfile(SRC, WS + r"\snn_app\src\helloworld.c")

client = vitis.create_client()
client.set_workspace(path=WS)
app = client.get_component(name="snn_app")
app.build()
print("BUILD CALL DONE")
vitis.dispose()

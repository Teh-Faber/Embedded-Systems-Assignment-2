import os
import vitis

HERE = os.path.dirname(os.path.abspath(__file__))
WS = os.path.join(HERE, "ws")
XSA = os.path.normpath(os.path.join(HERE, "..", "vivado", "snn_design.xsa"))

client = vitis.create_client()
client.set_workspace(path=WS)

platform = client.create_platform_component(
    name="snn_platform",
    hw_design=XSA,
    os="standalone",
    cpu="ps7_cortexa9_0",
    domain_name="standalone_ps7_cortexa9_0",
    generate_dtb=False,
)
platform.build()
print("PLATFORM BUILT")
vitis.dispose()


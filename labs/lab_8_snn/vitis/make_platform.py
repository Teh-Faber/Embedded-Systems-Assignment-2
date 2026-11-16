import vitis

WS = r"C:\Users\Bluex\Desktop\FPGA\lab_8_SNN\vitis_ws\ws"
XSA = r"C:\Users\Bluex\Desktop\FPGA\lab_8_SNN\vivado\snn_design.xsa"

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


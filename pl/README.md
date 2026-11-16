# pl

Everything after HLS: exported IP, the Vivado project and the Vitis workspace.

| Folder | Use |
|---|---|
| `ip/` | Exported HLS IP, one folder per IP (`ip/ds_cnn/`, `ip/snn_mlp/`). Point the Vivado IP repository here. |
| `vivado/` | Vivado project(s), block design scripts, exported XSA |
| `vitis/` | Vitis workspace: platform from the XSA and the app. Add the sources from `../ps/` as links instead of copying them. |
| `constraints/` | XDC files |

Generated project files are ignored by git (see `.gitignore`). Commit scripts, XDC and the XSA, not the project folders.
The HLS components themselves live in `../cnn/` and `../snn/` next to their `hls_config.cfg`.

# packaging/

Empty until the window exists. The plan of record is mainspring's chain: a PyInstaller onedir
build behind a per-user Inno Setup installer, built by a PowerShell script that cuts `PATH` down
for the build and asserts the window title on launch. The spec, the `.iss` and the build script
are copied from `../mainspring/packaging/` and `../mainspring/tools/build_exe.ps1` and adapted
when the application is packaged (lab record, task 08).

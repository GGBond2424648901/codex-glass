# Third-party notices

The original Codex Monitor application code retains the MIT notice in LICENSE. This distribution does not remove the rights or notices of upstream contributors.

## Desktop distribution

The executable combines the application with third-party components. Their licenses apply independently; the original MIT notice alone is not a license for the bundled PyQt runtime. The application source and build scripts are published alongside the executable. Preserve these notices when redistributing.

- **PyQt5 5.15.11** — Copyright Riverbank Computing Limited. Installed package metadata identifies GPL v3. Official licensing: https://www.riverbankcomputing.com/software/pyqt/ . Source: https://pypi.org/project/PyQt5/5.15.11/#files . The desktop combined distribution is provided under GPL-3.0 terms, with the original MIT permissions retained for the application sources.
- **Qt 5.15.2** — Copyright The Qt Company and contributors. Qt modules have LGPL/GPL and third-party notices; the installed Qt wheel license is included at LICENSES/Qt5.txt. Source: https://download.qt.io/archive/qt/5.15/5.15.2/single/ . Only the modules collected by the build are bundled.
- **PyQt5-sip 12.17.2** — Riverbank Computing, SIP runtime. License included at LICENSES/PyQt5-sip.txt. Source: https://pypi.org/project/PyQt5-sip/12.17.2/#files .
- **Python 3.13** — Python Software Foundation and contributors. License included at LICENSES/Python.txt. Source: https://www.python.org/downloads/source/ .
- **PyInstaller 6.22.2 bootloader** — GPL with the project's bootloader distribution exception. Included license at LICENSES/PyInstaller.txt. Source/build instructions: https://github.com/pyinstaller/pyinstaller/tree/v6.22.2 .

Corresponding application source, pinned desktop build dependencies and build_exe.ps1 are available in the same GitHub release. No commercial PyQt license is asserted. GNU GPL text: https://www.gnu.org/licenses/gpl-3.0.html .

The pulse icon is project artwork created for Codex Glass; it is not an OpenAI logo. OpenAI and Codex product names belong to their respective owners. This is an independent local monitoring utility, not an official billing product.

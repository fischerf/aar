## WSL Python environment

When running Python scripts that need third-party packages:
1. Create a virtual environment once: `python3 -m venv /tmp/aar_env`
2. Install packages into it: `/tmp/aar_env/bin/pip install <package>`
3. Run scripts with the venv Python: `/tmp/aar_env/bin/python3 script.py`

Always use `/tmp/aar_env/bin/python3` instead of bare `python` to ensure
packages installed in step 2 are available.

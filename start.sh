#!/bin/bash
# Wrapper script to launch caudalimetro with the correct X display.
# X may use :0 or :1 depending on boot, so we detect it dynamically.

# Find the active X display for this user
export DISPLAY=$(ls /tmp/.X11-unix/ | head -1 | sed 's/X/:/')
export XAUTHORITY=$(find /run/user/$(id -u) -name Xauthority 2>/dev/null | head -1)

# Fallbacks
[ -z "$DISPLAY" ] && export DISPLAY=:0
[ -z "$XAUTHORITY" ] && export XAUTHORITY="$HOME/.Xauthority"

exec /usr/bin/python3 /home/et-tambo/Caudalimetro/main.py

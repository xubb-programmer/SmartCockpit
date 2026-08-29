#!/bin/sh

if [ -r /etc/profile ]; then
    . /etc/profile
fi

if [ -r /etc/default/locale ]; then
    . /etc/default/locale
    export LANG LANGUAGE LC_ALL
fi

# 本机 GNOME 已自动登录；XRDP 使用独立的会话总线和虚拟 X11 桌面。
unset DBUS_SESSION_BUS_ADDRESS SESSION_MANAGER WAYLAND_DISPLAY
USER_ID="$(id -u)"
XRDP_RUNTIME_DIR="/tmp/xrdp-runtime-${USER_ID}"
install -d -m 0700 "${XRDP_RUNTIME_DIR}"
export XDG_RUNTIME_DIR="${XRDP_RUNTIME_DIR}"
export XDG_SESSION_TYPE=x11
export XDG_CURRENT_DESKTOP="ubuntu:GNOME"
export XDG_SESSION_DESKTOP=ubuntu
export GNOME_SHELL_SESSION_MODE=ubuntu
export GDK_BACKEND=x11
export LIBGL_ALWAYS_SOFTWARE=1
export GALLIUM_DRIVER=llvmpipe
export LD_LIBRARY_PATH="/lib/aarch64-linux-gnu:/usr/lib/aarch64-linux-gnu${LD_LIBRARY_PATH:+:${LD_LIBRARY_PATH}}"

exec dbus-run-session -- gnome-shell --x11 --sm-disable --mode=ubuntu

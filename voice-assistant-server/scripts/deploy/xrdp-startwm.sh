#!/bin/sh

if [ -r /etc/profile ]; then
    . /etc/profile
fi

if [ -r /etc/default/locale ]; then
    . /etc/default/locale
    export LANG LANGUAGE LC_ALL
fi

# 本机 GNOME 已自动登录；XRDP 使用独立的会话总线、运行目录和轻量桌面。
unset DBUS_SESSION_BUS_ADDRESS SESSION_MANAGER WAYLAND_DISPLAY
USER_ID="$(id -u)"
XRDP_RUNTIME_DIR="/tmp/xrdp-runtime-${USER_ID}"
install -d -m 0700 "${XRDP_RUNTIME_DIR}"
export XDG_RUNTIME_DIR="${XRDP_RUNTIME_DIR}"
export XDG_SESSION_TYPE=x11
export LIBGL_ALWAYS_SOFTWARE=1
export GALLIUM_DRIVER=llvmpipe

exec dbus-run-session -- sh -c '
    tint2 >/tmp/xrdp-tint2.log 2>&1 &
    xterm -geometry 110x32+40+40 >/tmp/xrdp-xterm.log 2>&1 &
    exec openbox-session
'

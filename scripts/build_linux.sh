#!/bin/sh
# Build the Linux AppImage inside the manylinux_2_34 image (AlmaLinux 9, glibc 2.34),
# so the result runs on every distribution with glibc 2.34 or newer.
#
# CI runs this inside that image from the checkout. Locally:
#
#   docker run --rm -v "$PWD:/src:ro" -v "$PWD/build/linux:/out" \
#       quay.io/pypa/manylinux_2_34_x86_64 sh /src/scripts/build_linux.sh
#
# With /src and /out mounted, the tracked files are copied into the container
# (the host's .venv is never touched) and the results land in /out.
set -eu

# The libraries Qt links against, so they can be found and bundled; patchelf
# rewrites library search paths. The development files and tools after them
# build the fcitx5 input method plugin (scripts/build_fcitx5_qt.py).
# A nearby mirror sometimes serves broken metadata. After a failure, switch from
# the location-based mirror list to AlmaLinux's own repository server.
# The image installs without documentation, where some packages keep their
# license: install with it, and reinstall the image's own packages that do so
# (scripts/notices.py fails on a bundled library without its license).
sed -i '/^tsflags=nodocs/d' /etc/dnf/dnf.conf
for attempt in 1 2 3; do
    if [ "$attempt" != 1 ]; then
        sed -i 's/^mirrorlist=/#mirrorlist=/; s/^# *baseurl=/baseurl=/' \
            /etc/yum.repos.d/almalinux*.repo
    fi
    if dnf install -y -q patchelf file \
        libxkbcommon-x11 xcb-util-cursor xcb-util-image xcb-util-keysyms \
        xcb-util-renderutil xcb-util-wm libEGL mesa-libGL fontconfig dbus-libs \
        libwayland-client libwayland-cursor libwayland-egl \
        cmake extra-cmake-modules gettext libxkbcommon-devel libxcb-devel libX11-devel \
        mesa-libGL-devel mesa-libEGL-devel wayland-devel >/dev/null &&
        dnf reinstall -y -q libXau libX11-common lz4-libs >/dev/null; then
        break
    fi
    [ "$attempt" = 3 ] && exit 1
    dnf clean all >/dev/null
    sleep 10
done

if ! command -v uv >/dev/null; then
    curl -LsSf https://astral.sh/uv/install.sh | sh
    PATH="$HOME/.local/bin:$PATH"
fi

if [ -d /src ] && [ -d /out ]; then
    git config --global --add safe.directory /src
    mkdir -p /work
    (cd /src && git ls-files -z --cached --others --exclude-standard | xargs -0 cp --parents -t /work)
    cd /work
fi

# The image's own Pythons have no libpython, which Nuitka links against.
export UV_PYTHON_PREFERENCE=only-managed UV_PROJECT_ENVIRONMENT=/tmp/stickle-venv
uv sync --locked
uv run python scripts/build.py

if [ -d /out ] && [ "$(pwd)" = /work ]; then
    rm -rf /out/stickle.dist /out/Stickle-x86_64.AppImage
    cp -r build/stickle.dist build/Stickle-x86_64.AppImage /out/
fi

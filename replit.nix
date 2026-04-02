{pkgs}: {
  deps = [
    pkgs.rsync
    pkgs.glib
    pkgs.libdrm
    pkgs.cups
    pkgs.expat
    pkgs.dbus
    pkgs.xorg.libX11
    pkgs.xorg.libxcb
    pkgs.xorg.libXfixes
    pkgs.xorg.libXrandr
    pkgs.xorg.libXdamage
    pkgs.xorg.libXcomposite
    pkgs.nspr
    pkgs.nss
    pkgs.gtk3
    pkgs.gdk-pixbuf
    pkgs.cairo
    pkgs.pango
    pkgs.alsa-lib
    pkgs.libxkbcommon
    pkgs.mesa
    pkgs.at-spi2-core
    pkgs.at-spi2-atk
    pkgs.atk
  ];
}

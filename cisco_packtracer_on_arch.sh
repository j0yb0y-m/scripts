#!/usr/bin/env bash
set -e

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

echo -e "${GREEN}=== Cisco Packet Tracer Manual Arch Installer ===${NC}\n"

# 1. Install extraction tools and common shared libraries required by the bundled Qt.
#    fuse2 is required because Packet Tracer 9.x ships its Linux binary as an
#    embedded AppImage, which needs libfuse.so.2 to run. fuse2 is NOT part of
#    Arch's base install, so it has to be pulled in explicitly.
echo -e "${YELLOW}[1/5] Installing extraction tools and dependencies...${NC}"
sudo pacman -S --needed --noconfirm binutils tar zstd nss alsa-lib libxss gtk3 nspr \
  libxcrypt-compat xdg-utils xcb-util-wm xcb-util-image xcb-util-keysyms \
  xcb-util-renderutil libxkbcommon-x11 fuse2

# 2. Locate the downloaded .deb file
echo -e "\n${YELLOW}[2/5] Locating Cisco Packet Tracer .deb file...${NC}"
DEB_FILE=$(find "$HOME/Downloads" "$PWD" -maxdepth 2 -type f -name "CiscoPacketTracer*.deb" 2>/dev/null | head -n 1)

if [ -z "$DEB_FILE" ]; then
  echo -e "${RED}[!] Cisco Packet Tracer .deb file not found!${NC}"
  echo -e "Please download the Ubuntu/Linux 64-bit .deb from NetAcad (or Skills For All) and place it in ~/Downloads."
  exit 1
fi

echo -e "${GREEN}Found installer file:${NC} $DEB_FILE"

# 3. Create a temporary extraction environment
TMP_DIR=$(mktemp -d -t pt-extract-XXXXXX)
echo -e "\n${YELLOW}[3/5] Extracting .deb archive in temporary directory...${NC}"
cd "$TMP_DIR"

# A .deb is an ar archive containing tarballs. 'ar' is provided by binutils.
ar x "$DEB_FILE"

# The application files are inside data.tar.xz or data.tar.zst depending on version
DATA_TAR=$(ls data.tar.*)
mkdir -p rootfs
tar -xf "$DATA_TAR" -C rootfs

# 4. Install files to the system root
echo -e "\n${YELLOW}[4/5] Moving files to /opt/pt and setting up integrations...${NC}"

# Remove any existing installation to avoid conflicts
sudo rm -rf /opt/pt

# Move the core application to /opt
sudo cp -R rootfs/opt/pt /opt/

# Copy desktop and icon files for app menu integration
if [ -d "rootfs/usr/share/applications" ]; then
  sudo cp -R rootfs/usr/share/applications/* /usr/share/applications/
fi
if [ -d "rootfs/usr/share/icons" ]; then
  sudo cp -R rootfs/usr/share/icons/* /usr/share/icons/
fi

# Find the actual executable inside /opt/pt instead of assuming a name.
# Packet Tracer 9.x ships "packettracer.AppImage"; older 8.x builds shipped a
# plain "packettracer" or "PacketTracer" binary. Detect whichever is present.
PT_BIN=$(find /opt/pt -maxdepth 1 -type f \( -iname "packettracer*" \) | sort | head -n 1)

if [ -z "$PT_BIN" ]; then
  echo -e "${RED}[!] Could not find a Packet Tracer executable inside /opt/pt.${NC}"
  echo -e "Contents of /opt/pt:"
  ls -la /opt/pt
  exit 1
fi

echo -e "${GREEN}Found executable:${NC} $PT_BIN"

# Create a system-wide executable symlink pointing at whatever was actually found
sudo ln -sf "$PT_BIN" /usr/bin/packettracer

# Register the pttp:// URI scheme so "launch exam" links on NetAcad open
# Packet Tracer directly from the browser (normally handled by the .deb's
# postinst script, which we don't run since we only unpack data.tar.*)
if [ -f "/usr/share/applications/cisco-ptsa.desktop" ]; then
  sudo xdg-mime default cisco-ptsa.desktop x-scheme-handler/pttp || true
fi

# Set proper execution permissions
sudo chmod -R 755 /opt/pt

# 5. Refresh system UI caches
echo -e "\n${YELLOW}[5/5] Updating desktop database and icon caches...${NC}"
sudo update-desktop-database /usr/share/applications || true
sudo gtk-update-icon-cache -q -t -f /usr/share/icons/hicolor || true

# Cleanup
cd ~
rm -rf "$TMP_DIR"

echo -e "\n${GREEN}=== Installation Complete! ===${NC}"
echo -e "To launch Packet Tracer, search for it in your application menu or type 'packettracer' in the terminal."
echo -e "${YELLOW}Note:${NC} first run may prompt you to accept the EULA on the command line before the GUI opens."
echo -e """
to uninstall:
sudo rm -rf /opt/pt
sudo rm /usr/bin/packettracer
sudo rm /usr/share/applications/cisco-pt.desktop
sudo rm /usr/share/applications/cisco-ptsa.desktop
"""

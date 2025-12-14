#!/bin/bash
# run_game.sh

# Setup Bluetooth Environment
# (Uncomment these if your PI needs to be forced into these modes)
# bluetoothctl <<EOF
# power on
# agent NoInputNoOutput
# default-agent
# pairable on
# discoverable on
# quit
# EOF

# Set environment variables for the C program
export LIBNFC_DEVICE=pn532_uart:/dev/ttyAMA1
export BT_ADDR=$(bluetoothctl show | grep "Controller" | awk '{print $2}')
export BT_ADDR_PIPE=/home/pi/Project/bt_addr_pipe

# Create the pipe if it doesn't exist
if [ ! -p $BT_ADDR_PIPE ]; then
    mkfifo $BT_ADDR_PIPE
fi

# Compile the NFC program (Just in case)
cd /home/pi/Project/libnfc/examples
make nfc-dep-initiator > /dev/null 2>&1

# Run the NFC executable in the background
./nfc-dep-initiator > /dev/null 2>&1 &
NFC_PID=$!

# Run the Battleship Python Script
cd /home/pi/Project
# Using sudo might be necessary for GPIO/Display access depending on your setup
sudo -E python3 battleship_nfc.py

# Cleanup: Kill the NFC process when Python exits
kill $NFC_PID
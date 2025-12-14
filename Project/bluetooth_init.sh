#   In the directory /home/pi/Project/libnfc/examples
#
#   nfc-dep-initiator.c compiles with the command "make nfc-dep-initiator" to the executable nfc-dep-initiator
#  
#   This program alternates between DEP (Data Exchange Protocol) initiator mode and DEP target mode, with a random
#   duration spent in target mode to ensure that both devices act in both modes. In initiator mode, the device sends
#   its bluetooth address to the other device if it encounters a device in target mode. When a bluetooth address is
#   sent from one device to the other, the receiving (target mode) device sends it to the BT_ADDR_PIPE pipe, which 
#   is then used by the Bluetooth program to connect to the advertising device as a client. 
#
#   There are a LOT of configuration settings that need to be changed for this program to work reliably so definitely
#   don't touch anything related to Bluetooth, UART, or nfclib if you can help it. 

#!/bin/bash
# Enable Bluetooth and configure agent

# bluetoothctl <<EOF
# power on
# agent NoInputNoOutput
# default-agent
# pairable on
# discoverable on
# quit
# EOF

# Set the libnfc configuration to use the UART-connected pn532 device
export LIBNFC_DEVICE=pn532_uart:/dev/ttyAMA1

# Set the bluetooth address as an environment variable, so the NFC program can use it
export BT_ADDR=$(bluetoothctl show | grep "Controller" | awk '{print $2}')

# Set the pipe for sending the bluetooth address from the NFC 
export BT_ADDR_PIPE=/home/pi/Project/bt_addr_pipe

cd /home/pi/Project/libnfc/examples
make nfc-dep-initiator

# Run the NFC executable with no output
./nfc-dep-initiator > /dev/null 2>&1 &

# Change the above line to the following if you need to see NFC code output
# ./nfc-dep-initiator &

NFC_PID=$!

cd /home/pi/Project

python3 bluetooth_peripheral.py

# Kill the NFC process after the python script has ended
kill $NFC_PID
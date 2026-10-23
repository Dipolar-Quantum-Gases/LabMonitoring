#### Lab Monitoring system

The monitoring system should be an easy system to connect and visualize the output of different sensors (Photodiiodes, Humidity sensors, Laser driver data, ...) 

The Sensor Monitor GUI includes a **Debug Console** button. Its diagnostics report
the application environment and configured channels, scan the I2C bus, and sample
all four ADS1115 inputs on each configured active board. A steady input is flagged
for inspection rather than declared faulty, since a constant sensor signal can be
normal. The test probes the I2C bus and samples ADC inputs; it does not configure
sensor inputs as GPIO outputs. Stop data collection before running diagnostics to
avoid competing for the ADC bus.

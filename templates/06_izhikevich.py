# Izhikevich Neurons — Two coupled neurons: driven + synaptically connected
import matplotlib
matplotlib.use('Agg')

import matplotlib.pyplot as plt
import numpy as np
from spinnaker2 import hardware, helpers, snn

print("Izhikevich Neuron Simulation")
print("-" * 40)
print("Neuron 1: driven by constant current (i_offset=20)")
print("Neuron 2: receives spikes from Neuron 1 + small offset current")

# Regular spiking Izhikevich parameters
izh_params_1 = {
    "recovery_decay":           0.02,
    "recovery_sensitivity":     0.2,
    "v_reset":                 -65.0,
    "reset_recovery_increment":  8.0,
    "i_offset":                 20.0,  # constant drive
}
izh_params_2 = {
    "recovery_decay":           0.02,
    "recovery_sensitivity":     0.2,
    "v_reset":                 -65.0,
    "reset_recovery_increment":  8.0,
    "i_offset":                  5.0,  # small offset
}

neuron1 = snn.Population(size=1, neuron_model="izhikevich",
                         params=izh_params_1, name="izh1", record=["spikes", "v"])
neuron2 = snn.Population(size=1, neuron_model="izhikevich",
                         params=izh_params_2, name="izh2", record=["spikes", "v"])

# Synaptic connection: neuron1 → neuron2
connections = [[0, 0, 15, 1]]
proj = snn.Projection(pre=neuron1, post=neuron2, connections=connections)

net = snn.Network("izhikevich_demo")
net.add(neuron1, neuron2, proj)

hw = hardware.SpiNNaker2Chip()
timesteps = 1000
print(f"Running {timesteps} timesteps on SpiNNaker2...")
hw.run(net, timesteps)
print("Done.")

spikes1   = neuron1.get_spikes()
spikes2   = neuron2.get_spikes()
voltages1 = neuron1.get_voltages()
voltages2 = neuron2.get_voltages()

print(f"Neuron 1 spike count: {sum(len(v) for v in spikes1.values())}")
print(f"Neuron 2 spike count: {sum(len(v) for v in spikes2.values())}")

fig, (ax1, ax2, ax3) = plt.subplots(3, 1, sharex=True,
                                     figsize=(10, 6), height_ratios=(1, 2, 1))

# Neuron 1 spikes
idx1, t1 = helpers.spike_times_dict_to_arrays(spikes1)
ax1.plot(t1, idx1, "|", ms=20, color="steelblue")
ax1.set_ylabel("Neuron 1\nSpikes")
ax1.set_ylim(-0.5, 0.5)

# Voltage traces
time = np.arange(timesteps)
ax2.plot(time, voltages1[0], label="Neuron 1 (driven)", color="steelblue")
ax2.plot(time, voltages2[0], label="Neuron 2 (syn input)", color="darkorange")
ax2.axhline(30, ls="--", c="0.5", label="Spike threshold (~30 mV)")
ax2.set_ylabel("Membrane voltage (mV)")
ax2.set_xlim(0, timesteps)
ax2.legend(fontsize=8)
ax2.set_title("Izhikevich Neuron Dynamics")

# Neuron 2 spikes
idx2, t2 = helpers.spike_times_dict_to_arrays(spikes2)
ax3.plot(t2, idx2, "|", ms=20, color="darkorange")
ax3.set_ylabel("Neuron 2\nSpikes")
ax3.set_xlabel("Timestep")
ax3.set_ylim(-0.5, 0.5)

fig.tight_layout()
plt.savefig("izhikevich_output.png", dpi=150)
print("Plot saved: izhikevich_output.png")

# LIF with Exponential Synaptic Current — Two neurons, exc/inh synapses
import matplotlib
matplotlib.use('Agg')

import matplotlib.pyplot as plt
import numpy as np
from spinnaker2 import hardware, helpers, snn

print("LIF Curr-Exp Simulation")
print("-" * 40)

# Input: single spike source
input_spikes = {0: [3, 10, 20, 22, 24, 26, 28]}
stim = snn.Population(size=1, neuron_model="spike_list", params=input_spikes, name="stim")

# Neuron parameters — lif_curr_exp model uses exponentially decaying currents
params = {
    "threshold":  10.0,
    "alpha_decay": 0.9,
    "i_offset":    0.0,
    "v_init":      0.0,
    "v_reset":    -0.2,
    "exc_decay":   0.5,
    "inh_decay":   0.2,
    "t_refract":   0,
    "reset": "reset_by_subtraction",
}

# Two neurons: one receives excitatory, other inhibitory input
pop1 = snn.Population(size=2, neuron_model="lif_curr_exp", params=params,
                      name="pop1", record=["spikes", "v"])

# Connections: [pre_index, post_index, weight, delay]
conns = [
    [0, 0,  2, 1],   # excitatory → neuron 0
    [0, 1, -3, 2],   # inhibitory → neuron 1
]
proj = snn.Projection(stim, pop1, conns)

net = snn.Network("lif_curr_exp_demo")
net.add(stim, pop1, proj)

hw = hardware.SpiNNcloud48NodeBoard()
timesteps = 50
print(f"Running {timesteps} timesteps on SpiNNaker2...")
hw.run(net, timesteps)
print("Done.")

spike_times = pop1.get_spikes()
voltages    = pop1.get_voltages()

print(f"Neuron 0 spikes: {spike_times.get(0, [])}")
print(f"Neuron 1 spikes: {spike_times.get(1, [])}")

fig, (ax1, ax2, ax3) = plt.subplots(3, 1, sharex=True,
                                     height_ratios=(1, 2, 1), figsize=(8, 6))
# Input spikes
indices, times = helpers.spike_times_dict_to_arrays(input_spikes)
ax1.plot(times, indices, "|", ms=20)
ax1.set_ylabel("Input spikes")
ax1.set_ylim((-0.5, 0.5))

# Voltage traces
t = np.arange(timesteps)
ax2.plot(t, voltages[0], label="Neuron 0 (exc)")
ax2.plot(t, voltages[1], label="Neuron 1 (inh)")
ax2.axhline(params["threshold"], ls="--", c="0.5", label="Threshold")
ax2.axhline(0, ls="-", c="0.8", zorder=0)
ax2.set_xlim(0, timesteps)
ax2.set_ylabel("Voltage")
ax2.legend(fontsize=8)

# Output spikes
indices, times = helpers.spike_times_dict_to_arrays(spike_times)
ax3.plot(times, indices, "|", ms=20)
ax3.set_ylabel("Output spikes")
ax3.set_xlabel("Timestep")
ax3.set_ylim((-0.5, 1.5))

fig.suptitle("LIF with Exponential Synaptic Currents")
plt.tight_layout()
plt.savefig("lif_curr_exp_output.png", dpi=150)
print("Plot saved: lif_curr_exp_output.png")

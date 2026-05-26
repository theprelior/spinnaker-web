# Poisson Process Neurons — Raster plot, firing rate & ISI distribution
import matplotlib
matplotlib.use('Agg')

import matplotlib.pyplot as plt
import numpy as np
from spinnaker2 import hardware, helpers, snn

print("Poisson Process Simulation")
print("-" * 40)

n_units = 250
neuron_params = {
    "p":    0.02,   # firing probability per timestep (≈ 20 Hz at 1 ms dt)
    "Tref": 3,      # 3 timestep refractory period
    "seed": 42,
}

print(f"Population: {n_units} Poisson neurons, p={neuron_params['p']}")

stim = snn.Population(
    size=n_units,
    neuron_model="poisson_process",
    params=neuron_params,
    name="stim",
    record=["spikes"],
)

net = snn.Network("poisson_demo")
net.add(stim)

hw = hardware.SpiNNcloud48NodeBoard()
timesteps = 1000
print(f"Running {timesteps} timesteps on SpiNNaker2...")
hw.run(net, timesteps)
print("Done.")

spike_times = stim.get_spikes()
indices, times = helpers.spike_times_dict_to_arrays(spike_times)

total_spikes = len(times)
mean_rate = total_spikes / n_units / (timesteps / 1000)
print(f"Total spikes: {total_spikes}")
print(f"Mean firing rate: {mean_rate:.1f} Hz")

rate_pop, bin_centers = helpers.calculate_population_spike_rate(times, n_units)

all_isis = []
for nid in range(n_units):
    st = times[indices == nid]
    if len(st) > 1:
        all_isis.extend(np.diff(st).tolist())

fig, (ax1, ax2, ax3) = plt.subplots(3, 1, figsize=(8, 8))

ax1.plot(times, indices, "|", ms=3, color="steelblue", alpha=0.6)
ax1.set_xlabel("Time (timesteps)")
ax1.set_ylabel("Neuron index")
ax1.set_title(f"Raster Plot  ({n_units} neurons, {timesteps} steps)")
ax1.set_xlim(0, timesteps)

ax2.plot(bin_centers, rate_pop, color="darkorange")
ax2.set_xlabel("Time (timesteps)")
ax2.set_ylabel("Firing rate (Hz)")
ax2.set_title("Population Firing Rate")
ax2.set_xlim(0, timesteps)

if all_isis:
    ax3.hist(all_isis, bins=50, density=True, color="seagreen", alpha=0.8, edgecolor="white")
    ax3.set_xlabel("Inter-spike interval (timesteps)")
    ax3.set_ylabel("Probability density")
    ax3.set_title("ISI Distribution (exponential = Poisson process)")

plt.tight_layout()
plt.savefig("poisson_output.png", dpi=150)
print("Plot saved: poisson_output.png")

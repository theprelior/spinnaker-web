# Adaptive LIF vs Standard LIF — Threshold adaptation reduces firing rate
import matplotlib
matplotlib.use('Agg')

import matplotlib.pyplot as plt
import numpy as np
from spinnaker2 import hardware, helpers, snn

print("Adaptive LIF vs Standard LIF")
print("-" * 40)
print("Same input, same base threshold — ALIF adapts threshold dynamically")

timesteps = 150
times_arr = np.arange(timesteps)

# Dense input spike train
spike_list = {
    0: list(range(1, timesteps, 3)),
    1: list(range(0, timesteps, 2)),
}
stim = snn.Population(size=2, neuron_model="spike_list", params=spike_list)

# Standard LIF
params_lif = {
    "threshold":  8.0,
    "alpha_decay": 0.9,
    "i_offset":    0.0,
    "v_init":      0.0,
    "v_reset":     0.0,
    "reset": "reset_to_v_reset",
}

# Adaptive LIF — threshold increases after each spike
params_alif = {
    **params_lif,
    "threshold_adp_decay": 0.99,   # slow decay of threshold increment
    "threshold_adp_inc":   5.0,    # threshold rises 5 units per spike
    "t_refract":           0,
}

pop_alif = snn.Population(size=1, neuron_model="alif", params=params_alif,
                           name="pop_alif", record=["spikes", "v", "v_th"])
pop_lif  = snn.Population(size=1, neuron_model="lif",  params=params_lif,
                           name="pop_lif",  record=["spikes", "v"])

conns = [[0, 0, 1, 0], [1, 0, 2, 0]]
proj_alif = snn.Projection(pre=stim, post=pop_alif, connections=conns)
proj_lif  = snn.Projection(pre=stim, post=pop_lif,  connections=conns)

net = snn.Network("alif_vs_lif")
net.add(stim, pop_alif, pop_lif, proj_alif, proj_lif)

hw = hardware.SpiNNcloud48NodeBoard()
print(f"Running {timesteps} timesteps on SpiNNaker2...")
hw.run(net, timesteps)
print("Done.")

spikes_alif = pop_alif.get_spikes()
spikes_lif  = pop_lif.get_spikes()
v_alif      = pop_alif.get_voltages()
v_lif       = pop_lif.get_voltages()
v_th_alif   = pop_alif.get_variable("v_th")

n_alif = sum(len(v) for v in spikes_alif.values())
n_lif  = sum(len(v) for v in spikes_lif.values())
print(f"ALIF spikes: {n_alif}  |  LIF spikes: {n_lif}")
print(f"Adaptation reduced firing by {100*(1 - n_alif/max(n_lif,1)):.0f}%")

fig, axes = plt.subplots(3, 2, sharex="col", figsize=(12, 6))
fig.suptitle("Adaptive LIF vs Standard LIF — Same Input, Same Base Threshold",
             fontsize=11, fontweight="bold")

# --- Input (shared) ---
idx_in, t_in = helpers.spike_times_dict_to_arrays(spike_list)
for ax in [axes[0, 0], axes[0, 1]]:
    ax.plot(t_in, idx_in, "|", ms=10, color="gray")
    ax.set_yticks([0, 1])
    ax.set_ylim(-0.5, 1.5)
    ax.set_ylabel("Input spikes")
axes[0, 0].set_title("ALIF (Adaptive threshold)", fontsize=10, fontweight="bold")
axes[0, 1].set_title("LIF (Fixed threshold)", fontsize=10, fontweight="bold")

# --- Voltages ---
axes[1, 0].plot(times_arr, v_th_alif[0], "--", c="tomato", label="Adaptive threshold", lw=1.5)
axes[1, 0].plot(times_arr, v_alif[0], label="Voltage", color="steelblue")
axes[1, 0].axhline(0, ls="-", c="0.8", zorder=0)
axes[1, 0].set_ylabel("Voltage / Threshold")
axes[1, 0].legend(fontsize=7)

axes[1, 1].axhline(params_lif["threshold"], ls="--", c="tomato", label="Fixed threshold", lw=1.5)
axes[1, 1].axhline(0, ls="-", c="0.8", zorder=0)
axes[1, 1].plot(times_arr, v_lif[0], label="Voltage", color="firebrick")
axes[1, 1].set_ylabel("Voltage / Threshold")
axes[1, 1].legend(fontsize=7)

# --- Output spikes ---
idx_a, t_a = helpers.spike_times_dict_to_arrays(spikes_alif)
idx_l, t_l = helpers.spike_times_dict_to_arrays(spikes_lif)
axes[2, 0].plot(t_a, idx_a, "|", ms=20, color="steelblue")
axes[2, 0].set_xlabel("Timestep")
axes[2, 0].set_ylabel(f"Output ({n_alif} spikes)")
axes[2, 0].set_ylim(-0.5, 0.5)

axes[2, 1].plot(t_l, idx_l, "|", ms=20, color="firebrick")
axes[2, 1].set_xlabel("Timestep")
axes[2, 1].set_ylabel(f"Output ({n_lif} spikes)")
axes[2, 1].set_ylim(-0.5, 0.5)

plt.tight_layout()
plt.savefig("adaptive_lif_output.png", dpi=150)
print("Plot saved: adaptive_lif_output.png")

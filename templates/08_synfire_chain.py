# Synfire Chain — Propagating activity through 5 groups of LIF neurons
import matplotlib
matplotlib.use('Agg')

import matplotlib.pyplot as plt
import numpy as np
from numpy.random import default_rng
from spinnaker2 import hardware, helpers, snn

print("Synfire Chain Simulation")
print("-" * 40)
print("Synchronous activity propagates through 5 neuron groups")
print("Reference: Höppner et al., IEEE TCAS-I (2019)")

rng = default_rng(42)

# Network parameters
n_groups    = 5
n_exc       = 100   # excitatory neurons per group
n_inh       = 25    # inhibitory neurons per group
n_pop       = n_exc + n_inh

params = {
    "threshold":  1167.67,
    "alpha_decay": 0.904837,
    "i_offset":    0.0,
    "v_reset":     0.0,
    "exc_decay":   0.513417119,
    "inh_decay":   0.36787944,
    "t_refract":   2,
    "reset": "reset_to_v_reset",
}

print(f"Groups: {n_groups}  |  Neurons/group: {n_pop} ({n_exc} exc + {n_inh} inh)")

# Initial stimulus — Gaussian-distributed spike times around t=25
input_times = rng.normal(25, 2, n_exc).astype(int).clip(1, 79)
input_spikes_dict = {i: [int(t)] for i, t in enumerate(input_times)}
stim = snn.Population(n_exc, "spike_list", input_spikes_dict, "stim")

# Create neuron groups
pops = []
for i in range(n_groups):
    pop = snn.Population(n_pop, "lif_curr_exp", params=params,
                         name=f"group_{i}", record=["spikes"])
    pop.set_max_atoms_per_core(125)
    pops.append(pop)

net = snn.Network("synfire_chain")
net.add(stim, *pops)

# Feed-forward excitatory connections (60 random pre-synaptic per neuron)
pre_pops = [stim] + pops[:-1]
for pre, post in zip(pre_pops, pops):
    conns = []
    for post_id in range(post.size):
        pre_ids = rng.choice(pre.size, size=min(60, pre.size), replace=False)
        for pre_id in pre_ids:
            conns.append([int(pre_id), post_id, 15.0, 7])
    net.add(snn.Projection(pre=pre, post=post, connections=conns))

# Local inhibition within each group (25 random inh pre-synaptic per exc neuron)
for pop in pops:
    conns = []
    for post_id in range(n_exc):
        pre_ids = rng.choice(range(n_exc, n_pop), size=min(25, n_inh), replace=False)
        for pre_id in pre_ids:
            conns.append([int(pre_id), post_id, -2.14, 5])
    net.add(snn.Projection(pre=pop, post=pop, connections=conns))

hw = hardware.SpiNNaker2Chip()
time_steps = 80
print(f"Running {time_steps} timesteps on SpiNNaker2...")
hw.run(net, time_steps)
print("Done.")

# Plot
fig, ax = plt.subplots(figsize=(10, 5))
colors = plt.cm.viridis(np.linspace(0.1, 0.9, n_groups))

for i, pop in enumerate(pops):
    spike_times = pop.get_spikes()
    indices, times = helpers.spike_times_dict_to_arrays(spike_times)
    n_spikes = len(times)
    print(f"  Group {i}: {n_spikes} spikes")
    if len(times):
        ax.plot(times, indices + i * n_pop, ".", ms=2,
                color=colors[i], label=f"Group {i} ({n_spikes} spikes)")

ax.set_xlim(0, time_steps)
ax.set_xlabel("Timestep")
ax.set_ylabel("Neuron index (offset by group)")
ax.set_title("Synfire Chain — Synchronous Activity Propagation")
ax.legend(fontsize=8, loc="upper right")
plt.tight_layout()
plt.savefig("synfire_chain_output.png", dpi=150)
print("Plot saved: synfire_chain_output.png")

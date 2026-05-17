# Leaky Integrate-and-Fire Neuron — Single neuron simulation
print("LIF Neuron Simulation")
print("-" * 40)

# Parameters
tau_m    = 20.0    # Membrane time constant (ms)
V_rest   = -70.0   # Resting potential (mV)
V_thresh = -55.0   # Threshold potential (mV)
V_reset  = -75.0   # Reset potential (mV)
R        = 10.0    # Membrane resistance (MΩ)
I_ext    = 2.0     # External current (nA)
dt       = 0.1     # Time step (ms)
T        = 500.0   # Simulation duration (ms)

V = V_rest
spike_times = []

for i in range(int(T / dt)):
    t  = i * dt
    dV = (-(V - V_rest) + R * I_ext) / tau_m * dt
    V += dV
    if V >= V_thresh:
        spike_times.append(round(t, 2))
        V = V_reset

n = len(spike_times)
print(f"Simulation duration : {T} ms")
print(f"Total spike count   : {n}")

if n > 1:
    isi = [spike_times[i+1] - spike_times[i] for i in range(n - 1)]
    mean_isi = sum(isi) / len(isi)
    print(f"Mean ISI            : {mean_isi:.1f} ms")
    print(f"Firing rate         : {n / (T / 1000):.1f} Hz")

print(f"\nFirst 10 spike times (ms):")
print("  ", spike_times[:10])
print("\nSimulation complete.")

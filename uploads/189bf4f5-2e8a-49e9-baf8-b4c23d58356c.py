# Leaky Integrate-and-Fire Nöron — Tek nöron simülasyonu
import math

print("LIF Nöron Simülasyonu")
print("-" * 40)

# Parametreler
tau_m    = 20.0    # Membran zaman sabiti (ms)
V_rest   = -70.0   # Dinlenme potansiyeli (mV)
V_thresh = -55.0   # Eşik potansiyeli (mV)
V_reset  = -75.0   # Reset potansiyeli (mV)
R        = 10.0    # Membran direnci (MΩ)
I_ext    = 2.0     # Dış akım (nA)
dt       = 0.1     # Zaman adımı (ms)
T        = 500.0   # Simülasyon süresi (ms)

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
print(f"Simülasyon süresi : {T} ms")
print(f"Toplam spike sayısı: {n}")

if n > 1:
    isi = [spike_times[i+1] - spike_times[i] for i in range(n - 1)]
    mean_isi = sum(isi) / len(isi)
    print(f"Ortalama ISI       : {mean_isi:.1f} ms")
    print(f"Ateşleme hızı      : {n / (T / 1000):.1f} Hz")

print(f"\nİlk 10 spike zamanı (ms):")
print("  ", spike_times[:10])
print("\nSimülasyon tamamlandı.")

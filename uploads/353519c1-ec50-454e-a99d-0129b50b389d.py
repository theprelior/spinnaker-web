# Rate Coding Analizi — Akım-frekans eğrisi (F-I curve)
import math

def lif_rate(I, tau=20, V_r=-70, V_th=-55, V_re=-75, R=10):
    """Sabit akım için analitik LIF ateşleme hızı (Hz)."""
    if I <= (V_th - V_r) / R:
        return 0.0
    T = tau * math.log((R * I + V_r - V_re) / (R * I + V_r - V_th))
    return 1000.0 / T

print("F-I Eğrisi (Akım → Ateşleme Hızı)")
print("=" * 45)
print(f"{'Akım (nA)':<12} {'Hız (Hz)':<10} Görsel")
print("-" * 45)

for I in [0.5, 1.0, 1.5, 2.0, 2.5, 3.0, 4.0, 5.0, 7.0, 10.0]:
    rate = lif_rate(I)
    bar  = "█" * int(rate / 5)
    mark = " ← eşik altı" if rate == 0 else ""
    print(f"{I:<12.1f} {rate:<10.1f} {bar}{mark}")

print("\nRheobase akımı: 1.5 nA (altında spike yok)")
print("Analiz tamamlandı.")

# Anti-Repeat Governance (Week 1)

Dokumen ini menetapkan disiplin eksperimen ringan agar riset Phase2/Phase4 tidak berulang tanpa sadar, sambil menjaga **PAPER_ONLY** tetap tegas.

## Ringkasan Audit Scope

Audit fokus pada artefak yang paling sering jadi sumber rerun:
- `docs/`
- `reports/`
- `scripts/`
- `phase2*/`
- `phase4*/`
- `walkforward*/`
- `calibration*/`
- `robustness*/`

Catatan audit operasional:
- Struktur repository saat ini sudah memiliki `docs/` dan `scripts/`.
- Folder pola lain (`reports/`, `phase2*`, `phase4*`, `walkforward*`, `calibration*`, `robustness*`) dapat muncul lintas iterasi dan tetap dipantau oleh helper.

## Pola Eksperimen yang Sering Berulang

Pola berisiko duplikasi:
1. **Threshold tuning**
2. **Calibration rerun**
3. **Robustness rerun**
4. **Temporal diagnosis**
5. **Nonlinear exploration**

## Kapan Eksperimen Boleh Diulang

Rerun diperbolehkan jika minimal salah satu kondisi benar:
- Ada **hipotesis baru yang eksplisit**.
- Ada **perubahan dataset window** yang material.
- Ada **target metric utama yang berbeda**.
- Ada **baseline_commit** baru yang relevan.

Syarat wajib:
- Entry registry baru harus dibuat (lihat schema).
- `paper_only_confirmed: true` wajib tercantum.
- Cantumkan alasan rerun di `governance_notes`.

## Kapan Wajib Supersede

Gunakan `supersedes` jika:
- Eksperimen baru menggantikan eksperimen lama (metode lebih baik/lebih valid).
- Hasil lama tidak lagi jadi referensi utama.
- Ada perbaikan desain eksperimen yang menjaga kompatibilitas tujuan.

Dampak:
- Entry lama tetap terlacak (audit trail).
- Entry baru menjadi referensi utama untuk keputusan berikutnya.

## Kapan Harus Reject Rerun

Rerun harus ditolak (`status: rejected`, isi `duplicate_of`) jika:
- Hipotesis sama.
- Dataset window sama atau praktis identik.
- Target metric sama.
- Baseline commit sama dan tidak ada justifikasi governance.

Tujuan reject:
- Mengurangi pemborosan siklus riset.
- Menjaga fokus ke eksperimen bernilai tambah.

## Governance Rationale

Layer ini sengaja ringan:
- Tidak menyentuh execution engine.
- Tidak mengubah strategy logic.
- Tidak retrain model.
- Fokus pada transparansi keputusan eksperimen dan disiplin dokumentasi.

Dengan metadata minimal + helper deteksi kandidat duplikasi, tim bisa cepat membedakan:
- eksperimen genuinely baru,
- eksperimen supersede,
- eksperimen yang semestinya ditolak.

## Kaitan dengan Source of Truth Policy

Dokumen ini melengkapi kebijakan Source of Truth agar:
- status eksperimen lebih mudah ditelusuri,
- keputusan rerun/supersede/reject terdokumentasi,
- kesinambungan riset tetap konsisten lintas iterasi.

Referensi utama kebijakan tetap pada dokumen Source of Truth repository, sementara anti-repeat governance berfungsi sebagai guardrail operasional eksperimen.

## PAPER_ONLY Enforcement

Seluruh workflow governance ini mengasumsikan dan menegaskan:
- **PAPER_ONLY harus tetap aktif**.
- Tidak ada implikasi auto-deploy/live trading dari artefak governance.

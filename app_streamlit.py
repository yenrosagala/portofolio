import os
import asyncio
import sqlite3
from datetime import datetime, timezone, timedelta
import pandas as pd
import streamlit as st
import matplotlib.pyplot as plt
from playwright.async_api import async_playwright
from newspaper import Article
from wordcloud import WordCloud
import base64
import re
import random

# Konfigurasi Awal Halaman Streamlit
st.set_page_config(page_title="Google News Scraper & Analisis Central", layout="wide")

# ==========================================
# KONFIGURASI PENGAMAN / PASSWORD LOGIN
# ==========================================
# Mengambil password dari Streamlit Secrets, jika belum disetel defaultnya adalah "admin123"
PASSWORD_RAHASIA = st.secrets.get("APP_PASSWORD", "admin123")

def cek_autentikasi():
    """Fungsi untuk memverifikasi sesi login pengguna"""
    if "autentikasi_sukses" not in st.session_state:
        st.session_state["autentikasi_sukses"] = False

    # Jika belum login, tampilkan formulir login khusus
    if not st.session_state["autentikasi_sukses"]:
        st.markdown("<br><br>", unsafe_allow_html=True)
        col_a, col_b, col_c = st.columns([1, 2, 1])
        with col_b:
            st.card = st.container(border=True)
            with st.card:
                st.subheader("🔒 Gerbang Keamanan Sistem")
                st.caption("Silakan masukkan kata sandi akses database untuk melanjutkan ke dasbor.")
                
                input_password = st.text_input("Kata Sandi (Password):", type="password", placeholder="Masukkan password Anda")
                tombol_login = st.button("Masuk ke Dasbor", type="primary", use_container_width=True)
                
                if tombol_login:
                    if input_password == PASSWORD_RAHASIA:
                        st.session_state["autentikasi_sukses"] = True
                        st.success("🔑 Akses diterima! Memuat halaman...")
                        st.rerun()
                    else:
                        st.error("❌ Kata sandi salah. Silakan coba lagi.")
        st.stop()  # Menghentikan rendering eksekusi kode di bawahnya sebelum login valid

# Jalankan proteksi keamanan di baris paling atas UI
cek_autentikasi()

# ==========================================
# CONTEXT MANAGER DATABASE (ONLINE / OFFLINE FALLBACK)
# ==========================================
DB_URL = st.secrets.get("DATABASE_URL", "berita_google_news.db")
IS_POSTGRES = DB_URL.startswith("postgresql://") or DB_URL.startswith("postgres://")

def dapatkan_koneksi_db():
    if IS_POSTGRES:
        import psycopg2
        return psycopg2.connect(DB_URL)
    else:
        return sqlite3.connect(DB_URL)

def inisialisasi_database():
    """Membuat tabel tunggal terpusat sesuai dialek SQL masing-masing"""
    conn = dapatkan_koneksi_db()
    cursor = conn.cursor()
    if IS_POSTGRES:
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS artikel (
                id SERIAL PRIMARY KEY,
                kata_kunci TEXT NOT NULL,
                judul TEXT NOT NULL,
                media TEXT,
                waktu_tampilan TEXT,
                waktu_iso TEXT,
                link TEXT UNIQUE, 
                isi_konten TEXT,
                di_scrap_pada TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
    else:
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS artikel (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                kata_kunci TEXT NOT NULL,
                judul TEXT NOT NULL,
                media TEXT,
                waktu_tampilan TEXT,
                waktu_iso TEXT,
                link TEXT UNIQUE, 
                isi_konten TEXT,
                di_scrap_pada TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
    conn.commit()
    conn.close()

def ambil_data_dari_db():
    """Mengambil seluruh database berita terpusat"""
    conn = dapatkan_koneksi_db()
    df = pd.read_sql_query("SELECT * FROM artikel ORDER BY id DESC", conn)
    conn.close()
    return df

inisialisasi_database()

# ==========================================
# 2. STRUKTUR KAMUS SENTIMEN KOMPLEKS (LEKSIKON)
# ==========================================
@st.cache_resource
def muat_kamus_sentimen_kompleks():
    kamus = {
        'sukses': 5, 'berhasil': 5, 'untung': 4, 'surplus': 5, 'tumbuh': 3, 'meningkat': 3, 
        'naik': 2, 'pulih': 4, 'optimal': 4, 'bagus': 3, 'baik': 3, 'aman': 4, 'stabil': 4, 
        'prestasi': 5, 'juara': 5, 'hebat': 4, 'unggul': 4, 'maju': 3, 'berkembang': 3, 
        'efektif': 4, 'efisien': 4, 'inovasi': 4, 'apresiasi': 4, 'mendukung': 3, 'setuju': 3, 
        'puas': 4, 'senang': 3, 'bahagia': 4, 'investasi': 2, 'bantuan': 3, 'membaik': 4,
        'gagal': -5, 'rugi': -4, 'krisis': -5, 'anjlok': -4, 'turun': -2, 'merosot': -4, 
        'korupsi': -5, 'suap': -5, 'pungli': -4, 'tewas': -5, 'korban': -4, 'inflasi': -3, 
        'defisit': -4, 'sanksi': -4, 'buruk': -4, 'bahaya': -4, 'ancaman': -4, 'hancur': -5, 
        'kecewa': -4, 'marah': -4, 'protes': -3, 'ditangkap': -3, 'tersangka': -4, 'lemah': -3
    }
    return kamus

def hitung_sentimen_leksikon(teks):
    if not teks or "[Gagal Ekstrak]" in teks:
        return "Netral"
    kamus_leksikon = muat_kamus_sentimen_kompleks()
    teks_bersih = teks.lower().replace('.', ' ').replace(',', ' ').replace('?', ' ').replace('!', ' ')
    kata_kata = teks_bersih.split()
    total_skor = 0
    for kata in kata_kata:
        if kata in kamus_leksikon:
            total_skor += kamus_leksikon[kata]
    if total_skor > 1: return "Positif"
    elif total_skor < -1: return "Negatif"
    else: return "Netral"

# ==========================================
# 3. CORE SCRAPER ENGINE 
# ==========================================
async def ekstrak_isi_berita_aman(context, url_google_news):
    page_tmp = None
    try:
        page_tmp = await context.new_page()
        await page_tmp.route("**/*", lambda route, request: 
            route.abort() if request.resource_type in ["image", "stylesheet", "font", "media"] else route.continue_()
        )
        await asyncio.sleep(random.uniform(1.0, 2.5))
        await page_tmp.goto(url_google_news, wait_until="commit", timeout=30000)
        await page_tmp.wait_for_timeout(random.uniform(3.0, 5.0) * 1000)
        
        url_asli = page_tmp.url
        if "google.com/sorry" in url_asli: return "[Gagal Ekstrak]: Terblokir CAPTCHA Google"
            
        html_konten = await page_tmp.content()
        await page_tmp.close()
        page_tmp = None
        
        artikel = Article(url_asli, language="id", keep_article_html=False)
        artikel.set_html(html_konten)
        artikel.parse()
        hasil_teks = artikel.text.strip()
        
        if not hasil_teks or len(hasil_teks) < 50:
            return "[Gagal Ekstrak]: Teks terlalu pendek atau dilindungi paywall"
        return hasil_teks
    except Exception as e:
        return f"[Gagal Ekstrak]: {str(e)}"
    finally:
        if page_tmp: await page_tmp.close()

async def run_scraper_pipeline(keyword, progress_bar, status_text):
    conn = dapatkan_koneksi_db()
    cursor = conn.cursor()
    jumlah_data_baru = 0
    
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        user_agent = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, Gecko) Chrome/120.0.0.0 Safari/537.36"
        context = await browser.new_context(user_agent=user_agent)
        page = await context.new_page()

        await page.goto("https://news.google.com/home?hl=id&gl=ID&ceid=ID:id")
        search_input_selector = "input[aria-label='Telusuri topik, lokasi & sumber']"
        await page.wait_for_selector(search_input_selector, timeout=10000)
        await page.click(search_input_selector)
        await page.type(search_input_selector, keyword, delay=50)
        await page.press(search_input_selector, "Enter")

        await page.wait_for_selector("a.JtKRv", timeout=15000)
        await page.wait_for_timeout(2000)
        berita_elements = await page.query_selector_all("a.JtKRv")
        
        waktu_sekarang = datetime.now(timezone.utc)
        batas_1_tahun = timedelta(days=365)
        total_target = len(berita_elements)

        for index, el in enumerate(berita_elements):
            progress_bar.progress((index + 1) / total_target)
            judul_raw = await el.inner_text()
            if not judul_raw or not judul_raw.strip(): continue
            
            link_raw = await el.get_attribute("href")
            link = "https://news.google.com" + link_raw[1:] if link_raw and link_raw.startswith(".") else link_raw
            status_text.text(f"⏳ [{index+1}/{total_target}] Memeriksa: {judul_raw[:40]}...")

            # Cek Duplikasi Tautan
            if IS_POSTGRES:
                cursor.execute("SELECT 1 FROM artikel WHERE link = %s", (link,))
            else:
                cursor.execute("SELECT 1 FROM artikel WHERE link = ?", (link,))
                
            if cursor.fetchone(): continue

            kotak_handle = await el.evaluate_handle("element => element.closest('c-wiz[data-node-index]') || element.closest('div.m5k28')")
            media, waktu_teks, waktu_iso = "Tidak ditemukan", "Tidak ditemukan", "Tidak ditemukan"

            if kotak_handle:
                kotak_element = kotak_handle.as_element()
                media_el = await kotak_element.query_selector("div.vr1PYe")
                if media_el: media = await media_el.inner_text()
                waktu_el = await kotak_element.query_selector("div.UOVeFe time.hvbAAd, time")
                if waktu_el:
                    waktu_teks = await waktu_el.inner_text()
                    waktu_iso = await waktu_el.get_attribute("datetime")

            if waktu_iso and waktu_iso != "Tidak ditemukan":
                try:
                    clean_iso = waktu_iso.replace("Z", "+00:00")
                    if (waktu_sekarang - datetime.fromisoformat(clean_iso)) > batas_1_tahun: break
                except Exception: pass

            isi_konten = await ekstrak_isi_berita_aman(context, link)
            judul = judul_raw.strip()

            # Filter Validasi Kata Kunci
            kata_kunci_bersih = keyword.lower().strip()
            if not (kata_kunci_bersih in judul.lower() or kata_kunci_bersih in isi_konten.lower()):
                continue

            try:
                # Penyimpanan Sesuai Placeholder Database
                if IS_POSTGRES:
                    cursor.execute('''
                        INSERT INTO artikel (kata_kunci, judul, media, waktu_tampilan, waktu_iso, link, isi_konten)
                        VALUES (%s, %s, %s, %s, %s, %s, %s) ON CONFLICT (link) DO NOTHING
                    ''', (keyword.strip(), judul, media, waktu_teks, waktu_iso, link, isi_konten))
                else:
                    cursor.execute('''
                        INSERT OR IGNORE INTO artikel (kata_kunci, judul, media, waktu_tampilan, waktu_iso, link, isi_konten)
                        VALUES (?, ?, ?, ?, ?, ?, ?)
                    ''', (keyword.strip(), judul, media, waktu_teks, waktu_iso, link, isi_konten))
                
                # Cek baris terpengaruh
                if IS_POSTGRES:
                    if cursor.rowcount > 0: jumlah_data_baru += 1
                else:
                    if conn.total_changes > 0: jumlah_data_baru += 1
            except Exception:
                pass

        conn.commit()
        conn.close()
        await browser.close()
    return jumlah_data_baru

# ==========================================
# 4. STREAMLIT APPLICATION INTERFACE (UI)
# ==========================================
st.title("Dasbor Analisis Media Terpusat & Google News Scraper 📊")

df_master = ambil_data_dari_db()

with st.sidebar:
    st.header("⚙️ Kontrol Panel Scraper")
    kata_kunci_input = st.text_input("Topik Baru untuk di-Scrap:", placeholder="Contoh: Sensus Ekonomi")
    tombol_tanam = st.button("Jalankan Sinkronisasi", type="primary", use_container_width=True)
    
    st.markdown("---")
    st.header("🎯 Filter Visualisasi Dasbor")
    if not df_master.empty:
        opsi_keyword = ["Semua Kata Kunci"] + list(df_master['kata_kunci'].unique())
        keyword_terpilih = st.selectbox("Pilih Data Kata Kunci:", opsi_keyword)
    else:
        keyword_terpilih = "Semua Kata Kunci"
        
    st.markdown("---")
    # Tombol Keluar untuk membersihkan session state login
    if st.button("🚪 Keluar / Log Out", use_container_width=True):
        st.session_state["autentikasi_sukses"] = False
        st.rerun()

if tombol_tanam and kata_kunci_input.strip():
    progress_bar = st.progress(0.0)
    status_text = st.empty()
    import sys
    if sys.platform == 'win32':
        asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
        
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        data_baru = loop.run_until_complete(run_scraper_pipeline(kata_kunci_input, progress_bar, status_text))
    finally:
        loop.close()
        
    progress_bar.empty()
    status_text.empty()
    st.success(f"✨ Sinkronisasi sukses! Berhasil menambahkan {data_baru} berita baru.")
    st.rerun()

if keyword_terpilih == "Semua Kata Kunci":
    df_aktual = df_master.copy()
else:
    df_aktual = df_master[df_master['kata_kunci'] == keyword_terpilih].copy()

tab1, tab2, tab3 = st.tabs(["📈 Analisis Grafik & Sentimen", "☁️ Word Cloud Konten", "🗃️ Data Tabel Database"])

with tab1:
    if not df_aktual.empty:
        col1, col2 = st.columns(2)
        with col1:
            st.subheader("Top 10 Media Teraktif")
            top_media = df_aktual['media'].value_counts().head(10).reset_index()
            top_media.columns = ['Nama Media', 'Jumlah Artikel']
            st.bar_chart(data=top_media, x='Nama Media', y='Jumlah Artikel', color="#1f77b4")
        with col2:
            st.subheader("Analisis Sentimen Kompleks (Leksikon Indonesia)")
            df_aktual['Sentimen'] = df_aktual['isi_konten'].apply(hitung_sentimen_leksikon)
            sentiment_counts = df_aktual['Sentimen'].value_counts().reset_index()
            sentiment_counts.columns = ['Sentimen', 'Total']
            st.bar_chart(data=sentiment_counts, x='Sentimen', y='Total', color="#ef553b")
    else:
        st.info("Database kosong. Silakan jalankan sinkronisasi data terlebih dahulu.")

with tab2:
    st.subheader("Awan Kata Konten Berita")
    if not df_aktual.empty:
        df_valid = df_aktual[df_aktual['isi_konten'].notna() & (~df_aktual['isi_konten'].str.contains(r'\[Gagal Ekstrak\]', case=False, na=False))]
        semua_teks = " ".join(df_valid['isi_konten'].astype(str))
        stopwords_id = {'yang', 'di', 'dan', 'itu', 'dengan', 'untuk', 'dari', 'seperti', 'ini', 'akan', 'dapat', 'oleh', 'ke', 'ada', 'adalah', 'sebuah', 'pada', 'tersebut', 'dalam', 'bisa', 'ia', 'juga', 'atau', 'telah'}
        if len(semua_teks.strip()) > 30:
            wordcloud = WordCloud(width=900, height=450, background_color='white', stopwords=stopwords_id, colormap='plasma').generate(semua_teks)
            fig, ax = plt.subplots(figsize=(12, 6))
            ax.imshow(wordcloud, interpolation='bilinear')
            ax.axis('off')
            st.pyplot(fig)
        else:
            st.warning("Volume teks belum mencukupi.")
    else:
        st.info("Jalankan sinkronisasi berita.")

with tab3:
    st.subheader("Daftar Rekam Data Berita")
    if not df_aktual.empty:
        if 'Sentimen' not in df_aktual.columns:
            df_aktual['Sentimen'] = df_aktual['isi_konten'].apply(hitung_sentimen_leksikon)
        st.dataframe(df_aktual[["kata_kunci", "judul", "media", "waktu_tampilan", "Sentimen", "link", "isi_konten"]].head(30), use_container_width=True)
    else:
        st.info("Belum ada record data.")

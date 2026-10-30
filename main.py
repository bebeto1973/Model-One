import os
import time
import io
import sqlite3
import requests
import pandas as pd
import numpy as np
import yfinance as yf
from datetime import timedelta
import warnings
warnings.filterwarnings('ignore')

print("🚀 AVVIO MOTORE QUANTITATIVO (Auto-Upsert su GitHub Actions)...")

# ==============================================================================
# 1. SETUP E RECUPERO TICKERS
# ==============================================================================
db_filename = 'sp500_bilanci_quarterly.db'
excel_filename = 'Database_Quantitativo_Aggiornato.xlsx'
headers = {'User-Agent': 'Andrea Galvan andrea.galvan@email.com'} 
conn = sqlite3.connect(db_filename)

print("📥 Estrazione Ticker e Settori (S&P 500)...")
url_wiki = 'https://en.wikipedia.org/wiki/List_of_S%26P_500_companies'
wiki_df = pd.read_html(io.StringIO(requests.get(url_wiki, headers=headers).text))[0]
df_settori = wiki_df[['Symbol', 'GICS Sector']].rename(columns={'Symbol': 'Ticker', 'GICS Sector': 'Settore'})
df_settori['Ticker'] = df_settori['Ticker'].str.replace('.', '-')
df_settori['Settore'] = df_settori['Settore'].replace({'Technology': 'Information Technology'})
tickers_list = df_settori['Ticker'].tolist()
df_settori.to_sql('raw_settori', conn, if_exists='replace', index=False)

tickers_json = requests.get("https://www.sec.gov/files/company_tickers.json", headers=headers).json()
ticker_to_cik = {v['ticker']: str(v['cik_str']).zfill(10) for k, v in tickers_json.items()}

# ==============================================================================
# 2. DOWNLOAD INCREMENTALE PREZZI (YAHOO FINANCE)
# ==============================================================================
print("📈 Controllo aggiornamento incrementale Prezzi...")
try:
    query = "SELECT MAX(Date) as Ultima_Data FROM raw_prezzi_giornalieri"
    ultima_data_db = pd.read_sql(query, conn)['Ultima_Data'].iloc[0]
    if ultima_data_db:
        data_partenza = pd.to_datetime(ultima_data_db) + timedelta(days=1)
    else:
        data_partenza = pd.to_datetime('2020-01-01')
except:
    data_partenza = pd.to_datetime('2020-01-01')

oggi = pd.Timestamp.today()
if data_partenza <= oggi:
    print(f"🔄 Scarico prezzi mancanti dal {data_partenza.strftime('%Y-%m-%d')} a oggi...")
    df_yfinance = yf.download(tickers_list, start=data_partenza.strftime('%Y-%m-%d'), end=(oggi + timedelta(days=1)).strftime('%Y-%m-%d'), interval='1d', auto_adjust=True, progress=False)
    
    if not df_yfinance.empty and 'Close' in df_yfinance:
        prezzi_close = df_yfinance['Close'].reset_index()
        # Se c'è un solo giorno/ticker, la struttura è diversa
        if isinstance(df_yfinance.columns, pd.MultiIndex):
            df_prezzi_long = pd.melt(prezzi_close, id_vars=['Date'], var_name='Ticker', value_name='Prezzo_Attuale').dropna()
        else:
            df_prezzi_long = pd.melt(prezzi_close, id_vars=['Date'], var_name='Ticker', value_name='Prezzo_Attuale').dropna()
            
        df_prezzi_long['Date'] = pd.to_datetime(df_prezzi_long['Date']).dt.tz_localize(None).dt.normalize().astype(str)
        df_prezzi_long.to_sql('raw_prezzi_giornalieri', conn, if_exists='append', index=False)
        print(f"✅ Accodati {len(df_prezzi_long)} nuovi record di prezzo.")
else:
    print("✅ Prezzi già aggiornati a oggi.")

# Ricostruiamo il dataframe prezzi per l'utilizzo in memoria
df_prezzi_long = pd.read_sql("SELECT * FROM raw_prezzi_giornalieri", conn)
df_prezzi_long['Date'] = pd.to_datetime(df_prezzi_long['Date'])
df_prezzi_long = df_prezzi_long.sort_values('Date')

# ==============================================================================
# 3. DOWNLOAD INCREMENTALE SEC (Solo se DB non esiste o è vecchio di 7gg per velocità)
# ==============================================================================
aggiorna_sec = True
try:
    query_sec = "SELECT MAX(Data) as Ultima_Data FROM bilanci_core_q"
    ultima_data_sec = pd.read_sql(query_sec, conn)['Ultima_Data'].iloc[0]
    if ultima_data_sec:
        # Aggiorna la SEC solo 1 volta a settimana per non consumare ore su GitHub Actions
        if pd.to_datetime(ultima_data_sec) > (oggi - timedelta(days=7)):
            aggiorna_sec = False
            print("⚡ Database SEC recente rilevato. Salto il download pesante.")
except:
    pass

voci_bilancio = {
    'Ricavi Totali': ['Revenues', 'SalesRevenueNet', 'SalesRevenueGoodsNet'],
    'Utile Netto': ['NetIncomeLossAvailableToCommonStockholdersBasic', 'NetIncomeLoss', 'ProfitLoss'],
    'Flusso Cassa Operativo': ['NetCashProvidedByUsedInOperatingActivities'],
    'Spese in Conto Capitale (CAPEX)': ['PaymentsToAcquirePropertyPlantAndEquipment'],
    'Patrimonio Netto': ['StockholdersEquity'],
    'Attività Correnti': ['AssetsCurrent'],
    'Passività Correnti': ['LiabilitiesCurrent'],
    'Passività Totali': ['Liabilities'],
    'EBITDA': ['OperatingIncomeLoss', 'EBITDA']
}

if aggiorna_sec:
    print(f"📡 Interrogazione server SEC per {len(tickers_list)} aziende (Tempo stimato: 10 min)...")
    all_data = []
    filings_data = []
    for i, ticker in enumerate(tickers_list, 1):
        cik = ticker_to_cik.get(ticker)
        if not cik: continue
        try:
            res_meta = requests.get(f"https://data.sec.gov/submissions/CIK{cik}.json", headers=headers)
            if res_meta.status_code == 200:
                df_rep = pd.DataFrame(res_meta.json()['filings']['recent'])
                df_rep = df_rep[df_rep['form'].isin(['10-K', '10-Q'])].copy()
                df_rep['Ticker'] = ticker
                filings_data.append(df_rep[['Ticker', 'reportDate', 'filingDate', 'form']])

            res_dati = requests.get(f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json", headers=headers)
            if res_dati.status_code == 200:
                usgaap = res_dati.json().get('facts', {}).get('us-gaap', {})
                for metrica, sinonimi in voci_bilancio.items():
                    for sin in sinonimi:
                        if sin in usgaap:
                            try:
                                unita = 'USD' if 'USD' in usgaap[sin]['units'] else list(usgaap[sin]['units'].keys())[0]
                                df_temp = pd.DataFrame(usgaap[sin]['units'][unita])
                                df_temp = df_temp[df_temp['form'].isin(['10-K', '10-Q'])].copy()
                                df_temp['end'] = pd.to_datetime(df_temp['end'])
                                df_temp = df_temp.sort_values('end').drop_duplicates(subset=['end'], keep='last')
                                df_temp['end'] = df_temp['end'].astype(str)
                                df_temp = df_temp[['end', 'val', 'fy', 'fp', 'form']]
                                df_temp['Ticker'] = ticker
                                df_temp['Metrica'] = metrica
                                all_data.append(df_temp)
                                break
                            except:
                                continue
        except: pass
        time.sleep(0.15)
        
    df_core = pd.concat(all_data, ignore_index=True).rename(columns={'end': 'Data', 'val': 'Valore', 'fy': 'Anno Fiscale', 'fp': 'Trimestre'})
    df_core.to_sql('bilanci_core_q', conn, if_exists='replace', index=False)
    pd.concat(filings_data, ignore_index=True).to_sql('raw_filing_dates_q', conn, if_exists='replace', index=False)
else:
    df_core = pd.read_sql("SELECT * FROM bilanci_core_q", conn)
    
# ==============================================================================
# 4. DECUMULAZIONE E MOTORE QUANTITATIVO (MASTER SCREENER)
# ==============================================================================
print("⚙️ Pulizia, Decumulazione e Costruzione Database Master...")
df_core['Data'] = pd.to_datetime(df_core['Data'])
df_core = df_core.sort_values(['Ticker', 'Metrica', 'Data', 'Anno Fiscale']).drop_duplicates(subset=['Ticker', 'Metrica', 'Data'], keep='last')

df_dates = df_core.groupby(['Ticker', 'Anno Fiscale', 'Trimestre'])['Data'].max().reset_index()
df_wide = df_core.pivot_table(index=['Ticker', 'Anno Fiscale', 'Trimestre'], columns='Metrica', values='Valore', aggfunc='last').reset_index()
df_wide.columns.name = None
df_wide = pd.merge(df_wide, df_dates, on=['Ticker', 'Anno Fiscale', 'Trimestre']).sort_values(['Ticker', 'Data']).drop_duplicates(subset=['Ticker', 'Data'], keep='last')

df_wide['Free Cash Flow YTD'] = df_wide.get('Flusso Cassa Operativo', 0) - df_wide.get('Spese in Conto Capitale (CAPEX)', 0).abs()
for met in ['Ricavi Totali', 'Utile Netto', 'Free Cash Flow YTD']:
    if met in df_wide.columns:
        df_wide[f"{met} (Discreto)"] = df_wide.groupby(['Ticker', 'Anno Fiscale'])[met].diff().fillna(df_wide[met])

df_filings = pd.read_sql("SELECT * FROM raw_filing_dates_q", conn)
df_filings['reportDate'] = pd.to_datetime(df_filings['reportDate'])
df_filings['filingDate'] = pd.to_datetime(df_filings['filingDate'])
df_filings = df_filings.sort_values('reportDate').drop_duplicates(subset=['Ticker', 'reportDate'], keep='last')

df = pd.merge_asof(
    df_wide.sort_values('Data'), 
    df_filings[['Ticker', 'reportDate', 'filingDate']].sort_values('reportDate'),
    left_on='Data', right_on='reportDate', by='Ticker', direction='nearest', tolerance=pd.Timedelta(days=15)
).dropna(subset=['filingDate'])

df['Data_Valutazione'] = df['filingDate']
df = df.sort_values('Data_Valutazione')
df = pd.merge_asof(df, df_prezzi_long.rename(columns={'Prezzo_Attuale': 'Prezzo'}), left_on='Data_Valutazione', right_on='Date', by='Ticker', direction='nearest')

df['Market Cap'] = df['Prezzo'] * 1e8 # Proxy per chiusura rapida senza azioni diluite
if 'Utile Netto (Discreto)' in df.columns: df['P/E (Run-Rate)'] = df['Market Cap'] / (df['Utile Netto (Discreto)'] * 4)
if 'Free Cash Flow YTD (Discreto)' in df.columns: df['P/FCF (Run-Rate)'] = df['Market Cap'] / (df['Free Cash Flow YTD (Discreto)'] * 4)
if 'Attività Correnti' in df.columns and 'Passività Correnti' in df.columns: df['Current Ratio (Disponibilità)'] = df['Attività Correnti'] / df['Passività Correnti']
if 'Patrimonio Netto' in df.columns and 'Passività Totali' in df.columns: df['Indice di Capitalizzazione'] = df['Patrimonio Netto'] / df['Passività Totali']

for c in [c for c in df.columns if '/' in c or 'Ratio' in c or 'Indice' in c]:
    df[c] = df[c].replace([np.inf, -np.inf], np.nan).round(2)

df = pd.merge(df, df_settori, on='Ticker', how='left')
df.to_excel(excel_filename, index=False)

# ==============================================================================
# 5. MACHINE LEARNING & ESTRAZIONE TOP 15 LIVE
# ==============================================================================
print("🧠 Applicazione Intelligenza Artificiale e Filtri...")
df['R1_FCF_Positivo'] = (df.get('Free Cash Flow YTD (Discreto)', 0) > 0).astype(int)
df['R2_Utile_Positivo'] = (df.get('Utile Netto (Discreto)', 0) > 0).astype(int)
df['R3_Liquidita_Sicura'] = (df.get('Current Ratio (Disponibilità)', 0) > 1.2).astype(int)
df['R4_PE_Conveniente'] = ((df.get('P/E (Run-Rate)', 0) > 0) & (df.get('P/E (Run-Rate)', 0) < 25)).astype(int)

df['Score_Dinamico_100'] = df['R1_FCF_Positivo']*30 + df['R2_Utile_Positivo']*30 + df['R3_Liquidita_Sicura']*20 + df['R4_PE_Conveniente']*20

def applica_hard_filters(row):
    if row.get('Current Ratio (Disponibilità)', 1) < 0.8 or row.get('Indice di Capitalizzazione', 1) < 0.15: return -1
    if row.get('Free Cash Flow YTD (Discreto)', 1) < 0 and row.get('Utile Netto (Discreto)', 1) < 0: return -1
    if row.get('P/E (Run-Rate)', 15) > 80 or row.get('P/E (Run-Rate)', 1) < 0: return -1
    return row['Score_Dinamico_100']

df['Score_Finale'] = df.apply(applica_hard_filters, axis=1)

# Estrazione Live (Ultimi 90 giorni)
data_limite = df['Data_Valutazione'].max() - timedelta(days=90)
df_live = df[(df['Data_Valutazione'] >= data_limite) & (df['Score_Finale'] >= 60)].copy()

df_live['FCF_Yield_%'] = (df_live.get('Free Cash Flow YTD (Discreto)', 0) * 4 / df_live.get('Market Cap', 1)) * 100
top_15 = df_live.sort_values(by=['Score_Dinamico_100', 'FCF_Yield_%'], ascending=[False, False]).head(15)

colonne_view = [c for c in ['Ticker', 'Settore', 'Data_Valutazione', 'Score_Dinamico_100', 'FCF_Yield_%', 'Prezzo', 'P/E (Run-Rate)'] if c in top_15.columns]
top_15[colonne_view].round(2).to_excel('Top_15_Live.xlsx', index=False)

conn.close()
print("🎉 PROCESSO COMPLETATO! Top_15_Live.xlsx generato per Alpaca.")

import sqlite3
import pandas as pd
import yfinance as yf
from datetime import timedelta

# 1. CONNESSIONE AL DATABASE ESISTENTE (Scaricato da GitHub Actions)
conn = sqlite3.connect('sp500_bilanci_quarterly.db')

try:
    # Cerchiamo la data più recente per cui abbiamo già scaricato i prezzi
    query = "SELECT MAX(Date) as Ultima_Data FROM raw_prezzi_giornalieri"
    ultima_data_db = pd.read_sql(query, conn)['Ultima_Data'].iloc[0]
    data_partenza = pd.to_datetime(ultima_data_db) + timedelta(days=1)
except:
    # Fallback se il DB è vuoto o nuovo
    data_partenza = pd.to_datetime('2020-01-01')

oggi = pd.Timestamp.today()

# 2. DOWNLOAD INCREMENTALE (Solo i giorni mancanti)
if data_partenza <= oggi:
    print(f"🔄 Aggiornamento DB: Scarico i dati mancanti dal {data_partenza.strftime('%Y-%m-%d')} a oggi...")
    
    # Scarica solo il pezzetto mancante
    nuovi_dati = yf.download(tickers_list, start=data_partenza.strftime('%Y-%m-%d'), end=oggi.strftime('%Y-%m-%d'))
    
    # 3. ACCODAMENTO AL DATABASE (APPEND)
    if non nuovi_dati.empty:
        # Formattazione...
        df_nuovi_prezzi.to_sql('raw_prezzi_giornalieri', conn, if_exists='append', index=False)
        print("✅ Nuovi prezzi accodati con successo.")
else:
    print("✅ Database già aggiornato a oggi. Nessun download necessario.")

# La stessa logica si applica alla SEC: controlliamo la 'filingDate' più recente nel DB
# e processiamo i JSON della SEC solo se c'è un nuovo form 10-K o 10-Q con data successiva.

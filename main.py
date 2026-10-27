import os
import logging
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
import pandas as pd
import numpy as np

from alpaca.trading.client import TradingClient
from alpaca.trading.requests import MarketOrderRequest
from alpaca.trading.enums import OrderSide, TimeInForce
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockLatestTradeRequest

# Setup Logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# Credenziali da GitHub Secrets
API_KEY = os.environ.get('ALPACA_API_KEY_MODEL_ONE')
API_SECRET = os.environ.get('ALPACA_SECRET_KEY_MODEL_ONE')
SENDER_EMAIL = os.environ.get('SENDER_EMAIL')
SENDER_PASSWORD = os.environ.get('SENDER_PASSWORD')
RECEIVER_EMAIL = os.environ.get('RECEIVER_EMAIL')

# Inizializzazione Client Alpaca
trading_client = TradingClient(API_KEY, API_SECRET, paper=True)
data_client = StockHistoricalDataClient(API_KEY, API_SECRET)

# ==========================================
# 1. & 2. AGGIORNAMENTO PREZZI VIA ALPACA
# ==========================================
def aggiorna_database_fondamentali(filepath='Database_Quantitativo_Definitivo.xlsx'):
    logger.warning("🔄 Avvio aggiornamento Database tramite Alpaca...")
    try:
        df = pd.read_excel(filepath)
        
        if 'Prezzo_Attuale' not in df.columns:
            df['Prezzo_Attuale'] = 0.0

        tickers = df['Ticker'].unique().tolist()
        chunk_size = 100
        
        for i in range(0, len(tickers), chunk_size):
            chunk_originali = tickers[i:i + chunk_size]
            # Converte trattini in punti per Alpaca (es. BF-B -> BF.B)
            chunk_alpaca = [str(t).replace('-', '.') for t in chunk_originali]
            
            try:
                req = StockLatestTradeRequest(symbol_or_symbols=chunk_alpaca)
                trades = data_client.get_stock_latest_trade(req)
                
                for ticker_alpaca, trade in trades.items():
                    # Riconverte il punto in trattino per salvare nel database originale
                    ticker_orig = ticker_alpaca.replace('.', '-')
                    df.loc[df['Ticker'] == ticker_orig, 'Prezzo_Attuale'] = float(trade.price)
            except Exception as e:
                logger.warning(f"⚠️ Errore download Alpaca per il blocco: {e}")
        
        df.to_excel(filepath, index=False)
        logger.warning(f"✅ Database aggiornato con gli ultimi prezzi per {len(tickers)} ticker.")
        return df
    except Exception as e:
        logger.warning(f"❌ Errore aggiornamento database: {e}")
        return None

# ==========================================
# 3. MOTORI QUANTITATIVI (MASTER SCREENER)
# ==========================================
def calcola_motori_quantitativi(df_master):
    logger.warning("⚙️ Calcolo Motori Quantitativi...")
    # Usa l'ultimo dato disponibile per ogni Ticker
    df_live = df_master.sort_values(by=['Ticker', 'Data']).groupby('Ticker').tail(1).copy()
    
    # Filtro base di sicurezza
    df_live = df_live[df_live['Prezzo_Attuale'] > 0].copy()
    
    azioni = df_live.get('Azioni Medie Base', 1).replace(0, 1)
    ricavi = df_live.get('Ricavi Totali (Discreto)', 1).replace(0, 1)
    market_cap = df_live['Prezzo_Attuale'] * azioni
    
    sbc = df_live.get('Stock-Based Compensation (SBC) (Discreto)', 0).fillna(0)
    fcf = df_live.get('Free Cash Flow YTD (Discreto)', 0).fillna(0)
    fcf_rett = fcf - sbc
    
    df_live['Rule of 40 (Proxy)'] = df_live.get('Crescita YoY (%)', 0) + ((fcf_rett / ricavi) * 100)
    df_live['FCF Yield'] = (fcf / market_cap) * 100
    df_live['Gross Margin (%)'] = (df_live.get('Utile Lordo (Discreto)', 0) / ricavi) * 100
    
    # --- MOTORE TECH ---
    tech = df_live[df_live['Settore'] == 'Information Technology'].copy()
    tech['Score'] = (tech['Rule of 40 (Proxy)'].rank(pct=True) * 0.70) + (tech['Gross Margin (%)'].rank(pct=True) * 0.30)
    tech = tech.dropna(subset=['Score']).sort_values(by='Score', ascending=False).head(5)
    
    # --- MOTORE DIFENSIVO ---
    difensivi = df_live[df_live['Settore'] != 'Information Technology'].copy()
    difensivi['Score'] = (difensivi['FCF Yield'].rank(pct=True) * 0.60) + (difensivi['Gross Margin (%)'].rank(pct=True) * 0.40)
    difensivi = difensivi.dropna(subset=['Score']).sort_values(by='Score', ascending=False).head(5)
    
    return pd.concat([tech, difensivi])

# ==========================================
# 4. ALLOCATORE BARBELL (LONG/SHORT)
# ==========================================
def crea_portafoglio_barbell(df_scored):
    logger.warning("⚖️ Creazione Asset Allocation Barbell...")
    df_long = df_scored.copy()
    df_long['Posizione'] = 'LONG'
    df_long['Peso (%)'] = 10.0  # 10% per ciascuno dei 10 titoli long
    return df_long

# ==========================================
# 5. ESECUZIONE ORDINI SU ALPACA
# ==========================================
def esegui_ordini_alpaca(df_portafoglio):
    logger.warning("🤖 Connessione ad Alpaca in corso...")
    try:
        logger.warning("🧹 Chiusura ordini e posizioni precedenti...")
        trading_client.cancel_orders()
        trading_client.close_all_positions(cancel_orders=True)
        
        account = trading_client.get_account()
        equity = float(account.equity)
        logger.warning(f"💰 Equity Disponibile: ${equity:,.2f}")
        
        ordini_eseguiti = []
        
        # --- ORDINI LONG ---
        for index, row in df_portafoglio.iterrows():
            ticker = row['Ticker'].replace('-', '.')
            prezzo = float(row['Prezzo_Attuale'])
            peso_decimale = row['Peso (%)'] / 100
            importo_dollari = equity * peso_decimale
            
            # Calcolo esplicito delle quote frazionate
            qty = round(importo_dollari / prezzo, 2) if prezzo > 0 else 0
            
            try:
                req = MarketOrderRequest(
                    symbol=ticker,
                    notional=round(importo_dollari, 2),
                    side=OrderSide.BUY,
                    time_in_force=TimeInForce.DAY
                )
                trading_client.submit_order(order_data=req)
                ordini_eseguiti.append({
                    'Ticker': ticker,
                    'Azione': 'BUY (Long)',
                    'Prezzo ($)': round(prezzo, 2),
                    'Quantità': qty,
                    'Importo ($)': round(importo_dollari, 2)
                })
            except Exception as e:
                logger.warning(f"❌ Errore ordine {ticker}: {e}")
                
        # --- ORDINI SHORT (SPY e QQQ) ---
        req_etf = StockLatestTradeRequest(symbol_or_symbols=['SPY', 'QQQ'])
        trades_etf = data_client.get_stock_latest_trade(req_etf)
        
        prezzo_spy = float(trades_etf['SPY'].price)
        prezzo_qqq = float(trades_etf['QQQ'].price)
        
        importo_short = equity * 0.50  # 50% short per ciascun ETF
        
        qty_spy = int(importo_short // prezzo_spy)
        qty_qqq = int(importo_short // prezzo_qqq)
        
        importo_spy = round(qty_spy * prezzo_spy, 2)
        importo_qqq = round(qty_qqq * prezzo_qqq, 2)
        
        try:
            if qty_spy > 0:
                req_spy = MarketOrderRequest(symbol='SPY', qty=qty_spy, side=OrderSide.SELL, time_in_force=TimeInForce.DAY)
                trading_client.submit_order(order_data=req_spy)
                ordini_eseguiti.append({
                    'Ticker': 'SPY',
                    'Azione': 'SELL (Short)',
                    'Prezzo ($)': round(prezzo_spy, 2),
                    'Quantità': qty_spy,
                    'Importo ($)': importo_spy
                })
        except Exception as e:
            logger.warning(f"❌ Errore ordine SPY: {e}")
            
        try:
            if qty_qqq > 0:
                req_qqq = MarketOrderRequest(symbol='QQQ', qty=qty_qqq, side=OrderSide.SELL, time_in_force=TimeInForce.DAY)
                trading_client.submit_order(order_data=req_qqq)
                ordini_eseguiti.append({
                    'Ticker': 'QQQ',
                    'Azione': 'SELL (Short)',
                    'Prezzo ($)': round(prezzo_qqq, 2),
                    'Quantità': qty_qqq,
                    'Importo ($)': importo_qqq
                })
        except Exception as e:
            logger.warning(f"❌ Errore ordine QQQ: {e}")
            
        return pd.DataFrame(ordini_eseguiti)
    except Exception as e:
        logger.warning(f"❌ Errore generale in esecuzione ordini: {e}")
        return None

# ==========================================
# 6. REPORT EMAIL
# ==========================================
def invia_email_report(df_ordini, df_portafoglio):
    if not SENDER_EMAIL or not SENDER_PASSWORD or not RECEIVER_EMAIL:
        logger.warning("❌ Credenziali Email mancanti, salto invio.")
        return
        
    try:
        msg = MIMEMultipart()
        msg['From'] = SENDER_EMAIL
        msg['To'] = RECEIVER_EMAIL
        msg['Subject'] = "📊 Report Barbell Quant - Esecuzione Completata"
        
        # Prepara la tabella dei fondamentali per la Top 10
        if df_portafoglio is not None and not df_portafoglio.empty:
            colonne_utili = ['Ticker', 'Settore', 'Score', 'Rule of 40 (Proxy)', 'FCF Yield', 'Gross Margin (%)']
            colonne_presenti = [c for c in colonne_utili if c in df_portafoglio.columns]
            
            df_fond = df_portafoglio[colonne_presenti].copy()
            
            # Formattazione per la mail (arrotondamenti e percentuali)
            if 'Score' in df_fond: df_fond['Score'] = df_fond['Score'].round(2)
            if 'Rule of 40 (Proxy)' in df_fond: df_fond['Rule of 40 (Proxy)'] = df_fond['Rule of 40 (Proxy)'].round(2).astype(str) + '%'
            if 'FCF Yield' in df_fond: df_fond['FCF Yield'] = df_fond['FCF Yield'].round(2).astype(str) + '%'
            if 'Gross Margin (%)' in df_fond: df_fond['Gross Margin (%)'] = df_fond['Gross Margin (%)'].round(2).astype(str) + '%'
            
            html_fondamentali = df_fond.to_html(index=False, border=1)
        else:
            html_fondamentali = "<p>Dati fondamentali non disponibili.</p>"
        
        html = f"""
        <html>
          <body>
            <h2>Resoconto Rebalancing Barbell Quantitativo</h2>
            <p>Il bot ha aggiornato il database, calcolato gli score, liquidato le posizioni ed eseguito i nuovi ordini con successo.</p>
            
            <h3>Ordini Inseriti a Mercato:</h3>
            {df_ordini.to_html(index=False, border=1) if df_ordini is not None and not df_ordini.empty else "<p>Nessun ordine eseguito.</p>"}
            
            <h3>Fondamentali Top 10 (Motivazioni di acquisto):</h3>
            {html_fondamentali}
          </body>
        </html>
        """
        msg.attach(MIMEText(html, 'html'))
        
        server = smtplib.SMTP('smtp.gmail.com', 587)
        server.starttls()
        server.login(SENDER_EMAIL, SENDER_PASSWORD)
        server.send_message(msg)
        server.quit()
        logger.warning("📧 Report email inviato con successo.")
    except Exception as e:
        logger.warning(f"❌ Errore invio email: {e}")

# ==========================================
# PIPELINE PRINCIPALE
# ==========================================
if __name__ == "__main__":
    df_aggiornato = aggiorna_database_fondamentali()
    if df_aggiornato is not None:
        df_scored = calcola_motori_quantitativi(df_aggiornato)
        df_portafoglio = crea_portafoglio_barbell(df_scored)
        df_ordini = esegui_ordini_alpaca(df_portafoglio)
        # Passiamo ENTRAMBI i dataframe per avere anche i fondamentali nella mail
        invia_email_report(df_ordini, df_portafoglio)
        df_portafoglio = crea_portafoglio_barbell(df_scored)
        df_ordini = esegui_ordini_alpaca(df_portafoglio)
        invia_email_report(df_ordini)

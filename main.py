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
            chunk_alpaca = [str(t).replace('-', '.') for t in chunk_originali]
            
            try:
                req = StockLatestTradeRequest(symbol_or_symbols=chunk_alpaca)
                trades = data_client.get_stock_latest_trade(req)
                
                for ticker_alpaca, trade in trades.items():
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
    df_live = df_master.sort_values(by=['Ticker', 'Data']).groupby('Ticker').tail(1).copy()
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
    df_long['Peso (%)'] = 10.0
    return df_long

# ==========================================
# 5. ESECUZIONE ORDINI A DELTA SU ALPACA
# ==========================================
def esegui_ordini_alpaca_delta(df_portafoglio):
    logger.warning("🤖 Connessione ad Alpaca e analisi posizioni attuali...")
    try:
        account = trading_client.get_account()
        equity = float(account.equity)
        logger.warning(f"💰 Equity Disponibile: ${equity:,.2f}")
        
        # Mappa le posizioni attualmente aperte su Alpaca
        posizioni_esistenti = {}
        for p in trading_client.get_all_positions():
            posizioni_esistenti[p.symbol] = {
                'qty': float(p.qty),
                'market_value': float(p.market_value),
                'unrealized_plpc': float(p.unrealized_plpc) * 100  # in percentuale
            }
            
        ordini_eseguiti = []
        simboli_target_long = [str(t).replace('-', '.') for t in df_portafoglio['Ticker'].tolist()]
        
        # 1. Chiusura posizioni Long non più in target
        for symbol in list(posizioni_esistenti.keys()):
            if symbol not in simboli_target_long and symbol not in ['SPY', 'QQQ']:
                try:
                    trading_client.close_position(symbol)
                    logger.warning(f"🔴 Chiusura posizione dismessa: {symbol}")
                except Exception as e:
                    logger.warning(f"⚠️ Errore chiusura {symbol}: {e}")

        # 2. Gestione ordini Long (Target 10% ciascuno)
        for index, row in df_portafoglio.iterrows():
            ticker = row['Ticker'].replace('-', '.')
            prezzo = float(row['Prezzo_Attuale'])
            target_importo = equity * 0.10
            target_qty = target_importo / prezzo if prezzo > 0 else 0
            
            qty_attuale = posizioni_esistenti.get(ticker, {}).get('qty', 0.0)
            delta_qty = target_qty - qty_attuale
            
            # Soglia minima di scostamento per evitare micro-transazioni inutili (es. 1% del target)
            if abs(delta_qty * prezzo) > (target_importo * 0.01):
                side = OrderSide.BUY if delta_qty > 0 else OrderSide.SELL
                qty_da_trattare = abs(round(delta_qty, 2))
                
                try:
                    req = MarketOrderRequest(
                        symbol=ticker,
                        qty=qty_da_trattare,
                        side=side,
                        time_in_force=TimeInForce.DAY
                    )
                    trading_client.submit_order(order_data=req)
                    azione_str = 'BUY (Delta)' if side == OrderSide.BUY else 'SELL (Delta)'
                    ordini_eseguiti.append({
                        'Ticker': ticker,
                        'Azione': azione_str,
                        'Prezzo ($)': round(prezzo, 2),
                        'Quantità': qty_da_trattare,
                        'Importo ($)': round(qty_da_trattare * prezzo, 2),
                        'PnL Inception (%)': f"{posizioni_esistenti.get(ticker, {}).get('unrealized_plpc', 0.0):.2f}%"
                    })
                except Exception as e:
                    logger.warning(f"❌ Errore ordine delta {ticker}: {e}")
            else:
                # Se la posizione è già bilanciata, la includiamo nel report con il suo PnL
                if ticker in posizioni_esistenti:
                    ordini_eseguiti.append({
                        'Ticker': ticker,
                        'Azione': 'HOLD (Bilanciato)',
                        'Prezzo ($)': round(prezzo, 2),
                        'Quantità': round(qty_attuale, 2),
                        'Importo ($)': round(qty_attuale * prezzo, 2),
                        'PnL Inception (%)': f"{posizioni_esistenti[ticker]['unrealized_plpc']:.2f}%"
                    })

        # 3. Gestione Short su SPY e QQQ (Target 50% ciascuno)
        req_etf = StockLatestTradeRequest(symbol_or_symbols=['SPY', 'QQQ'])
        trades_etf = data_client.get_stock_latest_trade(req_etf)
        
        for etf_symbol in ['SPY', 'QQQ']:
            prezzo_etf = float(trades_etf[etf_symbol].price)
            target_importo_etf = equity * 0.50
            target_qty_etf = int(target_importo_etf // prezzo_etf)
            
            qty_attuale_etf = posizioni_esistenti.get(etf_symbol, {}).get('qty', 0.0)
            # Nota: le quantità short su Alpaca sono negative nei registri delle posizioni
            delta_qty_etf = -target_qty_etf - qty_attuale_etf 
            
            if abs(delta_qty_etf) >= 1:
                side_etf = OrderSide.SELL if delta_qty_etf < 0 else OrderSide.BUY
                qty_trattata_etf = abs(int(delta_qty_etf))
                
                try:
                    req_etf_order = MarketOrderRequest(
                        symbol=etf_symbol,
                        qty=qty_trattata_etf,
                        side=side_etf,
                        time_in_force=TimeInForce.DAY
                    )
                    trading_client.submit_order(order_data=req_etf_order)
                    ordini_eseguiti.append({
                        'Ticker': etf_symbol,
                        'Azione': f"SELL/BUY (Short Delta)",
                        'Prezzo ($)': round(prezzo_etf, 2),
                        'Quantità': qty_trattata_etf,
                        'Importo ($)': round(qty_trattata_etf * prezzo_etf, 2),
                        'PnL Inception (%)': f"{posizioni_esistenti.get(etf_symbol, {}).get('unrealized_plpc', 0.0):.2f}%"
                    })
                except Exception as e:
                    logger.warning(f"❌ Errore ordine ETF {etf_symbol}: {e}")
            else:
                if etf_symbol in posizioni_esistenti:
                    ordini_eseguiti.append({
                        'Ticker': etf_symbol,
                        'Azione': 'HOLD (Short Bilanciato)',
                        'Prezzo ($)': round(prezzo_etf, 2),
                        'Quantità': abs(round(qty_attuale_etf, 2)),
                        'Importo ($)': round(abs(qty_attuale_etf) * prezzo_etf, 2),
                        'PnL Inception (%)': f"{posizioni_esistenti[etf_symbol]['unrealized_plpc']:.2f}%"
                    })
                    
        return pd.DataFrame(ordini_eseguiti).fillna('-')
    except Exception as e:
        logger.warning(f"❌ Errore generale in esecuzione delta ordini: {e}")
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
        msg['Subject'] = "📊 Report Barbell Quant (Delta Rebalancing) - Completato"
        
        if df_portafoglio is not None and not df_portafoglio.empty:
            colonne_utili = ['Ticker', 'Settore', 'Score', 'Rule of 40 (Proxy)', 'FCF Yield', 'Gross Margin (%)']
            colonne_presenti = [c for c in colonne_utili if c in df_portafoglio.columns]
            
            df_fond = df_portafoglio[colonne_presenti].copy()
            
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
            <h2>Resoconto Rebalancing Barbell (Delta & Performance)</h2>
            <p>Il bot ha analizzato le posizioni esistenti, calcolato i delta di ribilanciamento per minimizzare le transazioni e aggiornato il report.</p>
            
            <h3>Azioni ed Esecuzioni Delta:</h3>
            {df_ordini.to_html(index=False, border=1) if df_ordini is not None and not df_ordini.empty else "<p>Nessun movimento registrato.</p>"}
            
            <h3>Fondamentali Top 10 (Selezionate dal Modello):</h3>
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
        df_ordini = esegui_ordini_alpaca_delta(df_portafoglio)
        invia_email_report(df_ordini, df_portafoglio)

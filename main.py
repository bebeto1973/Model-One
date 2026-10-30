import os
import logging
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
import pandas as pd

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

trading_client = TradingClient(API_KEY, API_SECRET, paper=True)
data_client = StockHistoricalDataClient(API_KEY, API_SECRET)

def esegui_rebalancing_market_neutral():
    logger.info("🤖 Avvio Esecutore Market-Neutral...")
    ordini_eseguiti = []
    
    try:
        # 1. LETTURA DELLA TOP 15 DA GITHUB
        if not os.path.exists('Top_15_Live.xlsx'):
            logger.error("❌ File Top_15_Live.xlsx non trovato nel repository!")
            return None, None
            
        df_top15 = pd.read_excel('Top_15_Live.xlsx')
        tickers_long = [str(t).replace('.', '-') for t in df_top15['Ticker'].tolist()]
        
        if len(tickers_long) == 0:
            logger.error("❌ La Top 15 è vuota!")
            return None, None
        
        # 2. CALCOLO CAPITALE E TARGET BARBELL (1/15 per ciascun Long, 100% Short su SPY)
        account = trading_client.get_account()
        equity = float(account.equity)
        target_importo_long_singolo = equity / len(tickers_long)
        target_importo_short_spy = equity * 1.0 
        
        logger.info(f"💰 Equity Conto: ${equity:,.2f} | Target Singolo Long ({len(tickers_long)} titoli): ${target_importo_long_singolo:,.2f} | Target Short SPY: ${target_importo_short_spy:,.2f}")
        
        # 3. DOWNLOAD PREZZI IN TEMPO REALE DA ALPACA
        tutti_i_ticker = tickers_long + ['SPY']
        req = StockLatestTradeRequest(symbol_or_symbols=tutti_i_ticker)
        trades = data_client.get_stock_latest_trade(req)
        
        prezzi_live = {}
        for sym, trade in trades.items():
            if trade and hasattr(trade, 'price'):
                prezzi_live[sym] = float(trade.price)
                
        # 4. MAPPATURA POSIZIONI ATTUALI SU ALPACA
        posizioni_esistenti = {}
        for p in trading_client.get_all_positions():
            posizioni_esistenti[p.symbol] = {
                'qty': float(p.qty),
                'market_value': float(p.market_value),
                'unrealized_plpc': float(p.unrealized_plpc) * 100 if p.unrealized_plpc else 0.0
            }
            
        # ==========================================
        # FASE A: CHIUSURA POSIZIONI DISMESSE (Uscite dalla Top 15)
        # ==========================================
        for symbol in list(posizioni_esistenti.keys()):
            if symbol not in tickers_long and symbol != 'SPY':
                try:
                    trading_client.close_position(symbol)
                    logger.info(f"🔴 Chiusura posizione dismessa: {symbol}")
                except Exception as e:
                    logger.warning(f"⚠️ Errore chiusura {symbol}: {e}")

        # ==========================================
        # FASE B: ESECUZIONE GAMBA LONG
        # ==========================================
        for ticker in tickers_long:
            prezzo = prezzi_live.get(ticker, 0)
            if prezzo <= 0:
                logger.warning(f"⚠️ Prezzo non valido o non trovato per {ticker}, salto.")
                continue
            
            target_qty = target_importo_long_singolo / prezzo
            qty_attuale = posizioni_esistenti.get(ticker, {}).get('qty', 0.0)
            delta_qty = target_qty - qty_attuale
            
            # Soglia di tolleranza 1.5% per evitare micro-ordini per fluttuazioni minime
            if abs(delta_qty * prezzo) > (target_importo_long_singolo * 0.015):
                side = OrderSide.BUY if delta_qty > 0 else OrderSide.SELL
                qty_da_trattare = round(abs(delta_qty), 4) # Supporto azioni frazionate
                
                try:
                    req_long = MarketOrderRequest(symbol=ticker, qty=qty_da_trattare, side=side, time_in_force=TimeInForce.DAY)
                    trading_client.submit_order(order_data=req_long)
                    azione_str = 'BUY (Rebalance)' if side == OrderSide.BUY else 'SELL (Rebalance)'
                    ordini_eseguiti.append({
                        'Ticker': ticker, 'Gamba': 'LONG', 'Azione': azione_str,
                        'Prezzo ($)': round(prezzo, 2), 'Q.tà Delta': qty_da_trattare,
                        'Controvalore Delta ($)': round(qty_da_trattare * prezzo, 2),
                        'Esposizione Finale ($)': round(target_importo_long_singolo, 2)
                    })
                    logger.info(f"🟢 Ordine Long inviato: {side.name} {qty_da_trattare} {ticker}")
                except Exception as e:
                    logger.warning(f"❌ Errore ordine Long {ticker}: {e}")
            else:
                ordini_eseguiti.append({
                    'Ticker': ticker, 'Gamba': 'LONG', 'Azione': 'HOLD (Allineato)',
                    'Prezzo ($)': round(prezzo, 2), 'Q.tà Delta': 0,
                    'Controvalore Delta ($)': 0,
                    'Esposizione Finale ($)': round(posizioni_esistenti.get(ticker, {}).get('market_value', target_importo_long_singolo), 2)
                })

        # ==========================================
        # FASE C: ESECUZIONE GAMBA SHORT (Copertura SPY)
        # ==========================================
        prezzo_spy = prezzi_live.get('SPY', 0)
        if prezzo_spy > 0:
            target_qty_spy = int(target_importo_short_spy / prezzo_spy)
            qty_attuale_spy = posizioni_esistenti.get('SPY', {}).get('qty', 0.0) # Negativo se short
            
            delta_qty_spy = -target_qty_spy - qty_attuale_spy 
            
            if abs(delta_qty_spy) >= 1:
                side_spy = OrderSide.SELL if delta_qty_spy < 0 else OrderSide.BUY
                qty_trattata_spy = abs(int(delta_qty_spy))
                
                try:
                    req_spy = MarketOrderRequest(symbol='SPY', qty=qty_trattata_spy, side=side_spy, time_in_force=TimeInForce.DAY)
                    trading_client.submit_order(order_data=req_spy)
                    ordini_eseguiti.append({
                        'Ticker': 'SPY', 'Gamba': 'SHORT', 'Azione': f"{side_spy.name} (Hedge Delta)",
                        'Prezzo ($)': round(prezzo_spy, 2), 'Q.tà Delta': qty_trattata_spy,
                        'Controvalore Delta ($)': round(qty_trattata_spy * prezzo_spy, 2),
                        'Esposizione Finale ($)': round(target_importo_short_spy, 2)
                    })
                    logger.info(f"🛡️ Ordine Short SPY inviato: {side_spy.name} {qty_trattata_spy} quote")
                except Exception as e:
                    logger.warning(f"❌ Errore ordine SPY: {e}")
            else:
                ordini_eseguiti.append({
                    'Ticker': 'SPY', 'Gamba': 'SHORT', 'Azione': 'HOLD (Copertura Allineata)',
                    'Prezzo ($)': round(prezzo_spy, 2), 'Q.tà Delta': 0,
                    'Controvalore Delta ($)': 0,
                    'Esposizione Finale ($)': round(abs(posizioni_esistenti.get('SPY', {}).get('market_value', target_importo_short_spy)), 2)
                })

        return pd.DataFrame(ordini_eseguiti), df_top15
    except Exception as e:
        logger.error(f"❌ Fallimento critico esecutore: {e}")
        return None, None

def invia_email_esecutiva(df_ordini, df_top15):
    if not SENDER_EMAIL or not RECEIVER_EMAIL:
        logger.info("ℹ️ Configurazione email assente, salto invio notifica.")
        return
    try:
        msg = MIMEMultipart()
        msg['From'] = SENDER_EMAIL
        msg['To'] = RECEIVER_EMAIL
        msg['Subject'] = "⚙️ Esecuzione Quantitativa Market-Neutral Completata"
        
        html_ordini = df_ordini.to_html(index=False, border=1) if df_ordini is not None and not df_ordini.empty else "<p>Nessun movimento registrato.</p>"
        
        if df_top15 is not None:
            col_utili = [c for c in ['Ticker', 'Settore', 'Score_Dinamico_100', 'FCF_Yield_%', 'P/E (Run-Rate)'] if c in df_top15.columns]
            html_top15 = df_top15[col_utili].round(2).to_html(index=False, border=1)
        else:
            html_top15 = "<p>N/A</p>"
            
        html = f"""
        <html><body>
            <h2>Log di Esecuzione Market-Neutral (Alpaca)</h2>
            <p>Il bot ha allineato il portafoglio alla Top 15 (peso bilanciato 1/15) ed eseguito l'hedging 100% Short su SPY.</p>
            <h3>1. Transazioni ed Esposizione Odierna</h3>
            {html_ordini}
            <h3>2. Paniere Top 15 Attivo</h3>
            {html_top15}
        </body></html>
        """
        msg.attach(MIMEText(html, 'html'))
        server = smtplib.SMTP('smtp.gmail.com', 587)
        server.starttls()
        server.login(SENDER_EMAIL, SENDER_PASSWORD)
        server.send_message(msg)
        server.quit()
        logger.info("📧 Email di conferma inviata con successo.")
    except Exception as e:
        logger.warning(f"❌ Errore invio Email: {e}")

if __name__ == "__main__":
    df_movimenti, df_selezionati = esegui_rebalancing_market_neutral()
    invia_email_esecutiva(df_movimenti, df_selezionati)

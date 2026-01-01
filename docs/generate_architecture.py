#!/usr/bin/env python3
"""
Generate Sentient Trader Architecture Diagram
Creates a professional, recruiter-friendly system architecture diagram.
"""

import matplotlib.pyplot as plt
import matplotlib.patches as mpatches
from matplotlib.patches import FancyBboxPatch, FancyArrowPatch
import numpy as np

def create_architecture_diagram():
    # Create figure with dark professional theme
    fig, ax = plt.subplots(1, 1, figsize=(18, 12), facecolor='#1a1a2e')
    ax.set_facecolor('#1a1a2e')
    ax.set_xlim(0, 18)
    ax.set_ylim(0, 12)
    ax.axis('off')
    
    # Color palette
    colors = {
        'data_source': '#4ecdc4',      # Teal - Data Sources
        'kafka': '#ff6b6b',            # Coral - Kafka/Redpanda
        'worker': '#45b7d1',           # Blue - Workers
        'storage': '#96ceb4',          # Green - Storage
        'api': '#dda0dd',              # Plum - API
        'decision': '#ffd93d',         # Yellow - Decisions
        'text': '#ffffff',             # White text
        'arrow': '#888888',            # Gray arrows
    }
    
    def draw_box(x, y, w, h, color, label, sublabel=None, rounded=True):
        """Draw a styled box with label"""
        if rounded:
            box = FancyBboxPatch((x, y), w, h, 
                                  boxstyle="round,pad=0.02,rounding_size=0.3",
                                  facecolor=color, edgecolor='white', 
                                  linewidth=2, alpha=0.9)
        else:
            box = FancyBboxPatch((x, y), w, h,
                                  boxstyle="square,pad=0.02",
                                  facecolor=color, edgecolor='white',
                                  linewidth=2, alpha=0.9)
        ax.add_patch(box)
        
        # Main label
        ax.text(x + w/2, y + h/2 + (0.15 if sublabel else 0), label, 
                ha='center', va='center', fontsize=10, fontweight='bold',
                color='#1a1a2e', wrap=True)
        
        # Sublabel
        if sublabel:
            ax.text(x + w/2, y + h/2 - 0.2, sublabel,
                    ha='center', va='center', fontsize=8,
                    color='#333333', style='italic')
    
    def draw_kafka_topic(x, y, label):
        """Draw a Kafka topic (pill shape)"""
        box = FancyBboxPatch((x, y), 1.8, 0.5,
                              boxstyle="round,pad=0.02,rounding_size=0.25",
                              facecolor=colors['kafka'], edgecolor='white',
                              linewidth=1.5, alpha=0.9)
        ax.add_patch(box)
        ax.text(x + 0.9, y + 0.25, label, ha='center', va='center',
                fontsize=8, fontweight='bold', color='white')
    
    def draw_arrow(x1, y1, x2, y2, color=colors['arrow'], style='->', curved=False):
        """Draw an arrow between points"""
        if curved:
            arrow = FancyArrowPatch((x1, y1), (x2, y2),
                                     connectionstyle="arc3,rad=0.2",
                                     arrowstyle=style, color=color,
                                     linewidth=2, mutation_scale=15)
        else:
            ax.annotate('', xy=(x2, y2), xytext=(x1, y1),
                       arrowprops=dict(arrowstyle=style, color=color,
                                      linewidth=2, mutation_scale=15))
    
    # ========== TITLE ==========
    ax.text(9, 11.5, 'SENTIENT TRADER', ha='center', va='center',
            fontsize=24, fontweight='bold', color=colors['text'])
    ax.text(9, 11.0, 'Real-Time AI Trading Intelligence System',
            ha='center', va='center', fontsize=12, color='#aaaaaa')
    
    # ========== DATA SOURCES (Left) ==========
    ax.text(1.5, 10.2, 'DATA SOURCES', ha='center', va='center',
            fontsize=11, fontweight='bold', color=colors['data_source'])
    
    draw_box(0.5, 8.5, 2, 1.2, colors['data_source'], 'yfinance', '41 Stock Symbols')
    draw_box(0.5, 6.8, 2, 1.2, colors['data_source'], 'GNews API', 'Real-time News')
    draw_box(0.5, 5.1, 2, 1.2, colors['data_source'], 'NewsAPI', 'Financial Headlines')
    
    # ========== KAFKA / REDPANDA (Center-Left) ==========
    ax.text(5.5, 10.2, 'KAFKA (REDPANDA)', ha='center', va='center',
            fontsize=11, fontweight='bold', color=colors['kafka'])
    
    # Kafka topics
    draw_kafka_topic(4.6, 9.2, 'market-ticks')
    draw_kafka_topic(4.6, 8.2, 'news-stream')
    draw_kafka_topic(4.6, 7.2, 'news-with-tickers')
    draw_kafka_topic(4.6, 6.2, 'news-sentiment')
    draw_kafka_topic(4.6, 5.2, 'trade-decisions')
    
    # ========== WORKERS (Center) ==========
    ax.text(9, 10.2, 'WORKERS', ha='center', va='center',
            fontsize=11, fontweight='bold', color=colors['worker'])
    
    draw_box(7.5, 8.8, 3, 0.9, colors['worker'], 'market-data-producer', 'yfinance → ticks')
    draw_box(7.5, 7.6, 3, 0.9, colors['worker'], 'news-fetcher', 'GNews → news-stream')
    draw_box(7.5, 6.4, 3, 0.9, colors['worker'], 'ticker-extraction', 'NLP + spaCy NER')
    draw_box(7.5, 5.2, 3, 0.9, colors['worker'], 'sentiment-worker', 'FinBERT Analysis')
    draw_box(7.5, 4.0, 3, 0.9, colors['worker'], 'feature-worker', '27 Tech Indicators')
    
    # ========== STRATEGY ENGINE (Center-Right) ==========
    ax.text(13.5, 10.2, 'STRATEGY ENGINE', ha='center', va='center',
            fontsize=11, fontweight='bold', color=colors['decision'])
    
    draw_box(12, 7.5, 3, 2, colors['decision'], 'strategy-worker', 'Signal Fusion')
    
    # Decision outputs inside strategy box
    ax.text(13.5, 8.5, '[+] BUY', ha='center', va='center', fontsize=9,
            color='#00aa00', fontweight='bold')
    ax.text(13.5, 8.1, '[-] SELL', ha='center', va='center', fontsize=9,
            color='#dd0000', fontweight='bold')
            
    ax.text(13.5, 7.7, '[=] HOLD', ha='center', va='center', fontsize=9,
            color='#666666', fontweight='bold')
    
    # ========== STORAGE (Bottom) ==========
    ax.text(9, 2.8, 'STORAGE', ha='center', va='center',
            fontsize=11, fontweight='bold', color=colors['storage'])
    
    draw_box(6.5, 1.5, 2.5, 1, colors['storage'], 'Redis', 'Feature Store')
    draw_box(9.5, 1.5, 2.5, 1, colors['storage'], 'JSONL', 'Decision Logs')
    
    # ========== API (Right) ==========
    ax.text(16, 10.2, 'REST API', ha='center', va='center',
            fontsize=11, fontweight='bold', color=colors['api'])
    
    draw_box(15, 7.5, 2, 2, colors['api'], 'FastAPI', 'REST Endpoints')
    ax.text(16, 8.2, '/decisions', ha='center', va='center', fontsize=8, color='#333')
    ax.text(16, 7.8, '/insights', ha='center', va='center', fontsize=8, color='#333')
    
    # ========== ARROWS - Data Flow ==========
    # Data sources to Kafka
    draw_arrow(2.5, 9.1, 4.5, 9.45)  # yfinance -> market-ticks
    draw_arrow(2.5, 7.4, 4.5, 8.45)  # GNews -> news-stream
    draw_arrow(2.5, 5.7, 4.5, 8.35)  # NewsAPI -> news-stream
    
    # Kafka to Workers
    draw_arrow(6.5, 9.45, 7.4, 9.25)  # market-ticks -> market-data-producer
    draw_arrow(6.5, 8.45, 7.4, 8.05)  # news-stream -> news-fetcher
    draw_arrow(6.5, 7.45, 7.4, 6.85)  # news-with-tickers -> ticker-extraction
    draw_arrow(6.5, 6.45, 7.4, 5.65)  # news-sentiment -> sentiment-worker
    
    # Workers internal flow
    draw_arrow(9.25, 7.55, 9.25, 7.05)  # news-fetcher -> ticker-extraction
    draw_arrow(9.25, 6.35, 9.25, 5.85)  # ticker-extraction -> sentiment
    
    # To Strategy
    draw_arrow(10.5, 5.65, 12, 8.0)   # sentiment -> strategy
    draw_arrow(10.5, 4.45, 12, 8.2)   # features -> strategy
    
    # Strategy to outputs
    draw_arrow(15, 8.5, 15, 8.5)      # strategy -> API
    draw_arrow(13.5, 7.4, 10.75, 2.5) # strategy -> JSONL
    
    # Features to/from Redis
    draw_arrow(7.75, 3.9, 7.75, 2.6)  # feature-worker -> Redis
    draw_arrow(8.5, 2.5, 12, 7.4)     # Redis -> strategy
    
    # Kafka topic internal flow (vertical arrows on right side of topics)
    draw_arrow(6.5, 8.35, 6.5, 7.75)  # news-stream -> news-with-tickers
    draw_arrow(6.5, 7.15, 6.5, 6.75)  # news-with-tickers -> news-sentiment
    draw_arrow(6.5, 6.15, 6.5, 5.75)  # news-sentiment -> trade-decisions
    
    # ========== LEGEND ==========
    legend_y = 0.5
    legend_items = [
        (colors['data_source'], 'Data Sources'),
        (colors['kafka'], 'Kafka Topics'),
        (colors['worker'], 'Workers'),
        (colors['decision'], 'Decision Engine'),
        (colors['storage'], 'Storage'),
        (colors['api'], 'REST API'),
    ]
    
    for i, (color, label) in enumerate(legend_items):
        x = 1 + i * 2.8
        box = FancyBboxPatch((x, legend_y), 0.4, 0.3,
                              boxstyle="round,pad=0.02,rounding_size=0.1",
                              facecolor=color, edgecolor='white', linewidth=1)
        ax.add_patch(box)
        ax.text(x + 0.6, legend_y + 0.15, label, ha='left', va='center',
                fontsize=9, color=colors['text'])
    
    # ========== TECH STACK FOOTER ==========
    ax.text(9, 0.1, 'Tech: Python • FastAPI • Kafka/Redpanda • Redis • FinBERT • spaCy • yfinance • Docker',
            ha='center', va='center', fontsize=9, color='#666666')
    
    plt.tight_layout()
    return fig

if __name__ == '__main__':
    fig = create_architecture_diagram()
    
    # Save as PNG
    fig.savefig('docs/architecture.png', dpi=150, bbox_inches='tight',
                facecolor='#1a1a2e', edgecolor='none')
    print("✅ Saved: docs/architecture.png")
    
    # Save as SVG
    fig.savefig('docs/architecture.svg', bbox_inches='tight',
                facecolor='#1a1a2e', edgecolor='none')
    print("✅ Saved: docs/architecture.svg")
    
    plt.close()

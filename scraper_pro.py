import os
import re
import csv
import time
import json
import math
import queue
import requests
import threading
import platform
import subprocess
from concurrent.futures import ThreadPoolExecutor, as_completed
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from bs4 import BeautifulSoup
import tkinter as tk
from tkinter import ttk, messagebox, scrolledtext, font, filedialog
from datetime import datetime
from mysql.connector import pooling

try:
    from fpdf import FPDF
    HAS_FPDF = True
except ImportError:
    HAS_FPDF = False

# ── Constants ────────────────────────────────────────────────────────────────
DYNAMIC_WAIT_TIME = 15
WORKER_COUNT = 6       # number of parallel Chrome workers (product scrapers)
PAGE_CONCURRENCY = 4   # number of pages fetched from the API simultaneously

# ── Database Config ──────────────────────────────────────────────────────────
DB_CONFIG = {
    "host": "localhost",
    "user": "root",
    "password": "",           # <-- set your MySQL root password here
    "database": "mann_filter_db",
    "use_pure": True,         # avoid C-extension segfault on Python 3.14+
}

# ── API Config ───────────────────────────────────────────────────────────────
BASE_URL = "https://www.mann-filter.com/api/graphql/catalog-prod"

GRAPHQL_QUERY = """
query($search:String! $currentPage:Int! $pageSize:Int! $filterBy:TYPE_OF_FILTER) {
  catalogSearch:search_crossreference_no(
    search:$search
    currentPage:$currentPage
    pageSize:$pageSize
    filterBy:$filterBy
  ) {
    items {
      product {
        name
        sku
        attributes:attributes_value {
          key
          value
          __typename
        }
        __typename
      }
      externalNumber:external_number
      manufacturer:ext_brand_name
      filterBy:aa_product_family
      __typename
    }
    pageInfo:page_info {
      currentPage:current_page
      pageSize:page_size
      totalPages:total_pages
      __typename
    }
    totalCount:total_count
    __typename
  }
}
"""

# ── Color Palette (Modern Dark) ──────────────────────────────────────────────
NAVY = "#0C0C0F"
NAVY_LIGHT = "#16161D"
WHITE = "#F5F5F7"
BG_GRAY = "#101014"
CARD_BG = "#18181C"
CARD_BG_ALT = "#1E1E24"
BORDER = "#28282F"
BORDER_SUBTLE = "#222228"
INPUT_BG = "#1E1E24"
INPUT_BORDER = "#333340"
TEXT_PRIMARY = "#EAEAED"
TEXT_SECONDARY = "#7A7A88"
TEXT_DIM = "#55555F"
ACCENT_GREEN = "#34D399"
ACCENT_RED = "#F87171"
ACCENT_BLUE = "#60A5FA"
ACCENT_ORANGE = "#FBBF24"
ACCENT_GOLD = "#D4A843"
CONSOLE_BG = "#0C0C10"
CONSOLE_FG = "#C8C8D0"


# ═══════════════════════════════════════════════════════════════════════════════
#  SCRAPING LOGIC  (identical to app.py)
# ═══════════════════════════════════════════════════════════════════════════════

def extract_product_status(attributes):
    if not attributes:
        return "Unknown"
    for attr in attributes:
        if attr.get('key') == 'product_status_aa':
            return attr.get('value', 'Unknown')
    return "Unknown"


def cleanup_drivers():
    """Kill orphaned chromedriver processes (call once at start)."""
    system = platform.system().lower()
    if 'windows' in system:
        subprocess.run(['taskkill', '/F', '/IM', 'chromedriver.exe', '/T'],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    elif 'linux' in system or 'darwin' in system:
        subprocess.run(['pkill', '-f', 'chromedriver'],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def create_driver():
    """Create a new headless Chrome driver."""
    options = webdriver.ChromeOptions()
    options.add_argument('--headless')
    options.add_argument('--disable-gpu')
    options.add_argument('--no-sandbox')
    options.add_argument('--disable-dev-shm-usage')
    options.add_argument('--log-level=3')
    options.add_experimental_option('excludeSwitches', ['enable-logging'])
    options.add_argument('--disable-gcm')
    return webdriver.Chrome(options=options)


# Thread-local storage so each worker thread reuses its own driver
_thread_local = threading.local()


def get_thread_driver():
    """Get or create a driver for the current thread (reused across products)."""
    if not hasattr(_thread_local, 'driver') or _thread_local.driver is None:
        _thread_local.driver = create_driver()
    return _thread_local.driver


def close_thread_driver():
    """Close the driver for the current thread."""
    driver = getattr(_thread_local, 'driver', None)
    if driver:
        try:
            driver.quit()
        except Exception:
            pass
        _thread_local.driver = None


def get_page_content(url, driver):
    try:
        driver.get(url)
        WebDriverWait(driver, DYNAMIC_WAIT_TIME).until(
            EC.presence_of_element_located(
                (By.CSS_SELECTOR, ".cmp-product__title-name"))
        )
        WebDriverWait(driver, DYNAMIC_WAIT_TIME).until(
            EC.presence_of_element_located(
                (By.CSS_SELECTOR, ".cmp-table, .cmp-accordion"))
        )
        driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
        time.sleep(0.5)
        driver.execute_script("window.scrollTo(0, 0);")
        time.sleep(0.5)
        return driver.page_source
    except Exception as e:
        print(f"Error loading {url}: {e}")
        return None


def extract_product_data(soup, id, external_number, manufacturer, mann_filter,
                         status, filter_type, url):
    def clean_single_year(year_str):
        if not year_str:
            return ""
        year_str = re.sub(r'\D', '', year_str)
        if len(year_str) == 2:
            year_num = int(year_str)
            return f"19{year_str}" if year_num >= 30 and year_num < 100 else f"20{year_str}"
        if len(year_str) == 1:
            return f"190{year_str}" if int(year_str) >= 5 else f"200{year_str}"
        return year_str if len(year_str) == 4 else ""

    def clean_year(year_str):
        if not year_str:
            return ""
        year_str = re.sub(r'[^\d/-→]', '', year_str)
        if '-' in year_str:
            parts = year_str.split('-')
            return f"{clean_single_year(parts[0])}-{clean_single_year(parts[-1])}"
        if '→' in year_str:
            parts = year_str.split('→')
            return f"{clean_single_year(parts[0])}-{clean_single_year(parts[-1])}"
        return clean_single_year(year_str)

    def safe_text(element):
        return element.get_text(strip=True) if element else ''

    mann_filter_elem = soup.find('span', class_=lambda c: c and (
        'cmp-product__title-name' in c or
        'productFullDetail__productName' in c
    ))
    mann_filter = mann_filter_elem.get_text(strip=True) if mann_filter_elem else mann_filter

    data = {
        'id': id, 'external_number': external_number,
        'manufacturer': manufacturer, 'mann_filter': mann_filter,
        'status': status, 'filter_type': filter_type, 'url': url,
        'dimensions': {}, 'applications': {}, 'oem_numbers': []
    }

    # 1. Dimensions
    try:
        dimensions_section = (
            soup.find("div", id="dimensions") or
            soup.find("div", id="boyutlar") or
            soup.find(['h2', 'h3'], string=lambda t: t and
                      ('dimension' in t.lower() or 'boyut' in t.lower()))
        )
        if dimensions_section:
            if dimensions_section.name in ['h2', 'h3']:
                accordion = dimensions_section.find_parent("div", class_="cmp-accordion__item")
                if accordion:
                    dimensions_section = accordion.find("div", class_="cmp-accordion__panel")
            tables = []
            if dimensions_section:
                if dimensions_section.find("table"):
                    tables = dimensions_section.find_all("table")
                else:
                    for container in dimensions_section.find_all(class_="cmp-table__container"):
                        table = container.find("table")
                        if table:
                            tables.append(table)
            for table in tables:
                for row in table.find_all("tr"):
                    if not row.find("td"):
                        continue
                    cells = row.find_all("td")
                    if len(cells) == 2:
                        key, value = safe_text(cells[0]), safe_text(cells[1])
                        if key and value:
                            data['dimensions'][key] = value
                    elif len(cells) > 2:
                        key_cell = value_cell = None
                        for i, cell in enumerate(cells):
                            text = safe_text(cell).strip()
                            if len(text) == 1 and text.isalpha():
                                key_cell = cell
                                if i + 1 < len(cells):
                                    value_cell = cells[i + 1]
                                break
                        if key_cell and value_cell:
                            key, value = safe_text(key_cell), safe_text(value_cell)
                            if key and value:
                                data['dimensions'][key] = value
    except Exception as e:
        print(f"Error extracting dimensions: {e}")

    # 2. Applications
    try:
        applications_section = (
            soup.find("div", id="applications") or
            soup.find("div", id="uygulamalar") or
            soup.find("div", id="araclar") or
            soup.find("div", id="vehicles") or
            soup.find(lambda tag: tag.name in ['div', 'section'] and
                      any(kw in safe_text(tag).lower()
                          for kw in ['application', 'uygulama', 'araç', 'vehicle']))
        )
        if applications_section:
            brand_items = applications_section.find_all(
                "div", class_=lambda c: c and 'accordion__item' in c)
            for brand_item in brand_items:
                brand_name_elem = brand_item.find(class_=lambda c: c and 'accordion__title' in c)
                brand_name = safe_text(brand_name_elem)
                if not brand_name:
                    continue
                model_accordion = brand_item.find(class_=lambda c: c and 'accordion__nested' in c)
                if model_accordion:
                    model_items = model_accordion.find_all(
                        "div", class_=lambda c: c and 'accordion__item' in c)
                    for model_item in model_items:
                        model_name_elem = model_item.find(
                            class_=lambda c: c and 'accordion__title' in c)
                        model_name = safe_text(model_name_elem)
                        if not model_name:
                            continue
                        tables = model_item.find_all("table")
                        if tables:
                            if brand_name not in data['applications']:
                                data['applications'][brand_name] = {}
                            if model_name not in data['applications'][brand_name]:
                                data['applications'][brand_name][model_name] = []
                            for table in tables:
                                applications = []
                                headers = [safe_text(th).replace('\n', ' ').replace('\t', ' ').strip()
                                           for th in table.find_all("th") if safe_text(th)]
                                for row in table.find_all("tr")[1:]:
                                    cells = row.find_all("td")
                                    if len(cells) >= len(headers):
                                        app = {
                                            headers[i]: safe_text(cells[i]).replace('\n', ' ').replace('\t', ' ').strip()
                                            for i in range(min(len(headers), len(cells)))
                                        }
                                        if any(app.values()):
                                            applications.append(app)
                                if applications:
                                    for app in applications:
                                        year_key = 'Üretim yılı' if 'Üretim yılı' in app else 'Year of Manufacture'
                                        if year_key in app:
                                            app[year_key] = clean_year(app[year_key])
                                    data['applications'][brand_name][model_name].extend(applications)
    except Exception as e:
        print(f"Error extracting applications: {e}")

    # 3. OEM Numbers
    try:
        oem_section = (
            soup.find("div", id="oeNumbers") or
            soup.find("div", id="oemNumbers") or
            soup.find(lambda t: 'oem' in t.get('id', '').lower() or
                      'numara' in t.get('id', '').lower()) or
            soup.find(['h2', 'h3'], string=lambda t: t and
                      ('oem' in t.lower() or 'orijinal' in t.lower() or 'numara' in t.lower()))
        )
        if oem_section:
            if oem_section.name in ['h2', 'h3']:
                accordion = oem_section.find_parent("div", class_="cmp-accordion__item")
                if accordion:
                    oem_section = accordion.find("div", class_="cmp-accordion__panel")
            nested_accordion = oem_section.find("div", class_="cmp-accordion cmp-accordion__nested")
            if nested_accordion:
                for item in nested_accordion.find_all("div", class_="cmp-accordion__item"):
                    title_elem = item.find("span", class_="cmp-accordion__title")
                    manufacturer_name = safe_text(title_elem).strip() if title_elem else ""
                    panel = item.find("div", class_="cmp-accordion__panel")
                    if panel:
                        numbers = []
                        for ul in panel.find_all("ul"):
                            for li in ul.find_all("li"):
                                number = safe_text(li).strip()
                                if number and len(number) >= 3:
                                    numbers.append(number)
                        if numbers:
                            seen = set()
                            unique = [n for n in numbers if n not in seen and not seen.add(n)]
                            data['oem_numbers'].append({
                                'manufacturer': manufacturer_name,
                                'number': ', '.join(unique)
                            })
    except Exception as e:
        print(f"Error extracting OEM numbers: {str(e)}")

    return data



def process_url(id, external_number, manufacturer, mann_filter, status,
                filter_type, url):
    """Process a product URL using the thread-local reusable driver."""
    driver = get_thread_driver()
    try:
        html_content = get_page_content(url, driver)
        if not html_content:
            return None
        soup = BeautifulSoup(html_content, 'html.parser')
        return extract_product_data(soup, id, external_number, manufacturer,
                                    mann_filter, status, filter_type, url)
    except Exception as e:
        print(f"Error processing URL {url}: {str(e)}")
        # If the driver crashed, reset it so next call gets a fresh one
        close_thread_driver()
        return None


# ── MySQL Connection Pool (created once, reused by all threads) ──────────────
db_pool = None
try:
    db_pool = pooling.MySQLConnectionPool(
        pool_name="scraper_pool",
        pool_size=WORKER_COUNT + PAGE_CONCURRENCY + 2,
        **DB_CONFIG
    )
except Exception as _db_err:
    print(f"WARNING: Could not create DB pool: {_db_err}")


def save_to_db(product_data, search_term):
    """Insert a single product and its related data into MySQL."""
    if db_pool is None:
        raise RuntimeError("Database connection pool is not available")

    field_mapping = {
        "Model Tipi": "Model Type",
        "Filtre Tipi": "Filter Type",
        "Motor Kodu": "Engine Code",
        "Üretim yılı": "Year of Manufacture",
    }

    conn = db_pool.get_connection()
    cursor = conn.cursor()
    try:
        # Insert product
        cursor.execute(
            "INSERT INTO products (search_term, external_number, manufacturer, "
            "mann_filter, status, filter_type, url) VALUES (%s,%s,%s,%s,%s,%s,%s)",
            (search_term,
             product_data.get('external_number', ''),
             product_data.get('manufacturer', ''),
             product_data.get('mann_filter', ''),
             product_data.get('status', 'Unknown'),
             product_data.get('filter_type', ''),
             product_data.get('url', ''))
        )
        product_id = cursor.lastrowid

        # Insert dimensions
        dims = product_data.get('dimensions', {})
        if isinstance(dims, dict):
            for name, value in dims.items():
                cursor.execute(
                    "INSERT INTO dimensions (product_id, dimension_name, dimension_value) "
                    "VALUES (%s,%s,%s)", (product_id, name, value))

        # Insert vehicles / applications
        apps = product_data.get('applications', {})
        if isinstance(apps, dict):
            for brand, models in apps.items():
                if not isinstance(models, dict):
                    continue
                for model, app_list in models.items():
                    if not isinstance(app_list, list):
                        continue
                    for app in app_list:
                        if not isinstance(app, dict):
                            continue
                        mapped = {field_mapping.get(k, k): v for k, v in app.items()}
                        cursor.execute(
                            "INSERT INTO vehicles (product_id, brand, model, model_type, "
                            "filter_type, engine_code, ccm, kw, hp, year_of_manufacture) "
                            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                            (product_id, brand, model,
                             mapped.get('Model Type', ''),
                             mapped.get('Filter Type', ''),
                             mapped.get('Engine Code', ''),
                             mapped.get('ccm', ''),
                             mapped.get('kW', ''),
                             mapped.get('HP', ''),
                             mapped.get('Year of Manufacture', '')))

        # Insert OEM numbers
        oems = product_data.get('oem_numbers', [])
        if isinstance(oems, list):
            for entry in oems:
                if not isinstance(entry, dict):
                    continue
                mfr = entry.get('manufacturer', '')
                for num in [n.strip() for n in entry.get('number', '').split(',') if n.strip()]:
                    cursor.execute(
                        "INSERT INTO oem_numbers (product_id, manufacturer, oem_number) "
                        "VALUES (%s,%s,%s)", (product_id, mfr, num))

        conn.commit()
        return product_id
    except Exception as e:
        conn.rollback()
        raise e
    finally:
        cursor.close()
        conn.close()


# ═══════════════════════════════════════════════════════════════════════════════
#  GUI APPLICATION
# ═══════════════════════════════════════════════════════════════════════════════

class ScraperApp:
    def __init__(self):
        self.root = tk.Tk()
        self.root.title("MANN-FILTER Scraper Pro")
        self.root.minsize(800, 700)
        self.root.configure(bg=BG_GRAY)
        self.root.withdraw()  # hide main window until splash finishes

        # ── Fonts ────────────────────────────────────────────────────────
        self.font_title = font.Font(family="Segoe UI", size=16, weight="bold")
        self.font_subtitle = font.Font(family="Segoe UI Light", size=10)
        self.font_heading = font.Font(family="Segoe UI Semibold", size=11)
        self.font_label = font.Font(family="Segoe UI", size=10)
        self.font_entry = font.Font(family="Segoe UI", size=10)
        self.font_button = font.Font(family="Segoe UI Semibold", size=10)
        self.font_console = font.Font(family="Cascadia Mono", size=9)
        self.font_stat = font.Font(family="Segoe UI", size=9)
        self.font_metric_value = font.Font(family="Segoe UI", size=18, weight="bold")
        self.font_metric_label = font.Font(family="Segoe UI", size=8)

        # ── ttk Style ───────────────────────────────────────────────────
        self.style = ttk.Style()
        self.style.theme_use('clam')

        self.style.configure('.', background=BG_GRAY, foreground=TEXT_PRIMARY)
        self.style.configure('TFrame', background=BG_GRAY)
        self.style.configure('Card.TFrame', background=CARD_BG)
        self.style.configure('Card.TLabel', background=CARD_BG, font=self.font_label)
        self.style.configure('CardHeading.TLabel', background=CARD_BG,
                             font=self.font_heading, foreground=ACCENT_GOLD)
        self.style.configure('TLabel', background=BG_GRAY, font=self.font_label)

        # Slim modern progress bars
        self.style.configure('Horizontal.TProgressbar',
                             troughcolor='#222228', background=ACCENT_GOLD,
                             thickness=6, borderwidth=0)
        self.style.configure('Splash.Horizontal.TProgressbar',
                             troughcolor='#18181C', background=ACCENT_GOLD,
                             thickness=3, borderwidth=0)

        # Buttons — pill-style padding, modern feel
        _btn_pad = (20, 10)
        self.style.configure('Nav.TButton', font=self.font_button,
                             background=ACCENT_GOLD, foreground='#0C0C0F',
                             padding=_btn_pad, borderwidth=0)
        self.style.map('Nav.TButton',
                       background=[('active', '#C49B3A'), ('disabled', '#2A2A30')],
                       foreground=[('active', '#0C0C0F'), ('disabled', '#55555F')])

        self.style.configure('Danger.TButton', font=self.font_button,
                             background='#2D1520', foreground=ACCENT_RED,
                             padding=_btn_pad, borderwidth=0)
        self.style.map('Danger.TButton',
                       background=[('active', '#3E1E2A'), ('disabled', '#1E1E22')],
                       foreground=[('active', ACCENT_RED), ('disabled', '#55555F')])

        self.style.configure('Help.TButton', font=self.font_button,
                             background='#2A2A32', foreground=WHITE,
                             padding=_btn_pad, borderwidth=0)
        self.style.map('Help.TButton',
                       background=[('active', '#36363E')],
                       foreground=[('active', WHITE)])

        self.style.configure('Data.TButton', font=self.font_button,
                             background='#1A2840', foreground='#8BB8FF',
                             padding=_btn_pad, borderwidth=0)
        self.style.map('Data.TButton',
                       background=[('active', '#223454')],
                       foreground=[('active', '#A8CFFF')])

        # Menubutton styles — high-contrast text, compact for headers
        _mbtn_pad = (14, 6)
        self.style.configure('Data.TMenubutton', font=self.font_button,
                             background='#1A2840', foreground='#8BB8FF',
                             padding=_mbtn_pad, borderwidth=0)
        self.style.map('Data.TMenubutton',
                       background=[('active', '#223454')],
                       foreground=[('active', '#A8CFFF')])
        self.style.configure('Nav.TMenubutton', font=self.font_button,
                             background=ACCENT_GOLD, foreground='#0C0C0F',
                             padding=_mbtn_pad, borderwidth=0)
        self.style.map('Nav.TMenubutton',
                       background=[('active', '#C49B3A')],
                       foreground=[('active', '#0C0C0F')])

        self.style.configure('Retry.TButton', font=self.font_button,
                             background='#302010', foreground='#FFD060',
                             padding=_btn_pad, borderwidth=0)
        self.style.map('Retry.TButton',
                       background=[('active', '#3E2C18'), ('disabled', '#1E1E22')],
                       foreground=[('active', '#FFD060'), ('disabled', '#55555F')])

        # Combobox
        self.style.configure('TCombobox',
                             fieldbackground=INPUT_BG, background=INPUT_BORDER,
                             foreground=TEXT_PRIMARY, arrowcolor=ACCENT_GOLD,
                             selectbackground=INPUT_BORDER,
                             selectforeground=TEXT_PRIMARY,
                             borderwidth=0, padding=4)
        self.style.map('TCombobox',
                       fieldbackground=[('readonly', INPUT_BG)],
                       foreground=[('readonly', TEXT_PRIMARY)],
                       selectbackground=[('readonly', INPUT_BORDER)],
                       selectforeground=[('readonly', TEXT_PRIMARY)])
        self.root.option_add('*TCombobox*Listbox.background', INPUT_BG)
        self.root.option_add('*TCombobox*Listbox.foreground', TEXT_PRIMARY)
        self.root.option_add('*TCombobox*Listbox.selectBackground', ACCENT_GOLD)
        self.root.option_add('*TCombobox*Listbox.selectForeground', '#0C0C0F')

        # Notebook tabs
        self.style.configure('TNotebook', background=CARD_BG, borderwidth=0)
        self.style.configure('TNotebook.Tab',
                             background='#1E1E24', foreground=TEXT_DIM,
                             padding=(16, 7),
                             font=self.font_label, borderwidth=0)
        self.style.map('TNotebook.Tab',
                       background=[('selected', '#28282F'), ('active', '#24242A')],
                       foreground=[('selected', ACCENT_GOLD), ('active', TEXT_PRIMARY)],
                       expand=[('selected', [0, 0, 0, 2])])

        # Scrollbar (dark)
        self.style.configure('Vertical.TScrollbar',
                             background='#24242A', troughcolor=CARD_BG,
                             arrowcolor=TEXT_DIM, borderwidth=0, width=10)
        self.style.map('Vertical.TScrollbar',
                       background=[('active', '#333340')])
        self.style.configure('Horizontal.TScrollbar',
                             background='#24242A', troughcolor=CARD_BG,
                             arrowcolor=TEXT_DIM, borderwidth=0, width=10)
        self.style.map('Horizontal.TScrollbar',
                       background=[('active', '#333340')])

        # ── Show Splash, then build main UI ──────────────────────────────
        self._show_splash()

    def _show_splash(self):
        """Show a modern preloader splash screen."""
        _sp_bg = '#08080C'
        self._splash = tk.Toplevel(self.root)
        self._splash.title("")
        self._splash.overrideredirect(True)
        self._splash.configure(bg=_sp_bg)

        # Center the splash on screen
        sw, sh = 460, 300
        sx = (self.root.winfo_screenwidth() - sw) // 2
        sy = (self.root.winfo_screenheight() - sh) // 2
        self._splash.geometry(f"{sw}x{sh}+{sx}+{sy}")
        self._splash.attributes('-topmost', True)

        # Thin gold top accent
        tk.Frame(self._splash, bg=ACCENT_GOLD, height=1).pack(fill=tk.X)

        inner = tk.Frame(self._splash, bg=_sp_bg)
        inner.pack(fill=tk.BOTH, expand=True)

        # Spacer
        tk.Frame(inner, bg=_sp_bg, height=50).pack()

        # Brand
        tk.Label(inner, text="MANN-FILTER",
                 font=font.Font(family="Segoe UI", size=26, weight="bold"),
                 bg=_sp_bg, fg=ACCENT_GOLD).pack()
        tk.Label(inner, text="S C R A P E R   P R O",
                 font=font.Font(family="Segoe UI Light", size=10),
                 bg=_sp_bg, fg=TEXT_DIM).pack(pady=(4, 0))

        # Decorative line
        tk.Frame(inner, bg=BORDER_SUBTLE, height=1).pack(
            fill=tk.X, padx=100, pady=24)

        # Status text
        self._splash_status = tk.Label(
            inner, text="Initializing...",
            font=font.Font(family="Segoe UI", size=9),
            bg=_sp_bg, fg=TEXT_DIM)
        self._splash_status.pack()

        # Progress bar
        self._splash_progress = ttk.Progressbar(
            inner, orient=tk.HORIZONTAL, length=260, mode='determinate',
            style='Splash.Horizontal.TProgressbar', maximum=100)
        self._splash_progress.pack(pady=(14, 0))

        # Spacer
        tk.Frame(inner, bg=_sp_bg, height=24).pack()

        # Footer
        tk.Label(inner, text="Powered by Spiderhunts Technologies Ltd",
                 font=font.Font(family="Segoe UI Light", size=7),
                 bg=_sp_bg, fg='#333340').pack()

        # Bottom gold accent line
        tk.Frame(self._splash, bg=ACCENT_GOLD, height=1).pack(
            fill=tk.X, side=tk.BOTTOM)

        self._splash.update()
        self.root.after(200, lambda: self._splash_step(0))

    def _splash_step(self, step):
        """Run splash loading steps sequentially."""
        steps = [
            (10,  "Loading modules..."),
            (25,  "Configuring interface..."),
            (45,  "Checking database connection..."),
            (65,  "Building UI components..."),
            (80,  "Initializing workers..."),
            (95,  "Almost ready..."),
            (100, "Launching..."),
        ]

        if step >= len(steps):
            self._finish_splash()
            return

        progress, text = steps[step]
        self._splash_progress['value'] = progress
        self._splash_status.config(text=text)
        self._splash.update()

        # Step 2 (index 2) actually tests DB
        if step == 2:
            try:
                if db_pool is None:
                    raise Exception("No pool")
                conn = db_pool.get_connection()
                conn.close()
                self._splash_status.config(text="Database connected", fg=ACCENT_GREEN)
            except Exception:
                self._splash_status.config(text="Database unavailable — continuing",
                                           fg=ACCENT_ORANGE)
            self._splash.update()

        # Step 3: build the actual UI
        if step == 3:
            self._build_header()
            self._build_body()

        # Step 4: set up state
        if step == 4:
            self._init_state()

        delay = 350 if step < len(steps) - 1 else 500
        self.root.after(delay, lambda: self._splash_step(step + 1))

    def _finish_splash(self):
        """Destroy splash and show the main window."""
        self._splash.destroy()
        del self._splash
        self.root.deiconify()
        self.root.state('zoomed')  # maximize after showing

    def _init_state(self):
        """Initialize app state and console tags (called from splash)."""
        # ── State ────────────────────────────────────────────────────────
        self.is_scraping = False
        self.should_stop = False
        self.start_time = None
        self.processed_count = 0
        self.saved_count = 0
        self.error_count = 0
        self.failed_products = []

        # Worker slot tracking (thread-safe queue of free slot indices)

        self._worker_slots_q = queue.Queue()
        for i in range(WORKER_COUNT):
            self._worker_slots_q.put(i)
        self._thread_to_slot = {}  # thread_id -> slot_index

        # Console tags
        self.console.tag_config('error', foreground='#FF6B6B')
        self.console.tag_config('warning', foreground='#FFD93D')
        self.console.tag_config('success', foreground='#6BCB77')
        self.console.tag_config('info', foreground='#7B8FA8')
        self.console.tag_config('timestamp', foreground=ACCENT_GOLD)

        self._suppress_logs()

    # ── Header ───────────────────────────────────────────────────────────
    def _build_header(self):
        header = tk.Frame(self.root, bg=NAVY, height=56)
        header.pack(fill=tk.X)
        header.pack_propagate(False)

        # Thin gold accent line
        tk.Frame(header, bg=ACCENT_GOLD, height=1).pack(fill=tk.X, side=tk.TOP)

        # Left: branding
        brand = tk.Frame(header, bg=NAVY)
        brand.pack(side=tk.LEFT, padx=24, pady=8)
        tk.Label(brand, text="MANN-FILTER", font=self.font_title,
                 bg=NAVY, fg=ACCENT_GOLD).pack(side=tk.LEFT)
        tk.Label(brand, text="  Scraper Pro", font=self.font_subtitle,
                 bg=NAVY, fg=TEXT_DIM).pack(side=tk.LEFT, pady=(4, 0))

        # Right: status indicators
        right_box = tk.Frame(header, bg=NAVY)
        right_box.pack(side=tk.RIGHT, padx=24)

        # DB status pill
        db_box = tk.Frame(right_box, bg='#18181C', padx=8, pady=2)
        db_box.pack(side=tk.LEFT, padx=(0, 10))
        self.db_dot = tk.Canvas(db_box, width=8, height=8,
                                bg='#18181C', highlightthickness=0)
        self.db_dot.pack(side=tk.LEFT, padx=(0, 5))
        self._check_db_connection()
        tk.Label(db_box, text="Database", font=self.font_stat,
                 bg='#18181C', fg=TEXT_DIM).pack(side=tk.LEFT)

        # Scraper status pill
        status_box = tk.Frame(right_box, bg='#18181C', padx=8, pady=2)
        status_box.pack(side=tk.LEFT)
        self.status_dot = tk.Canvas(status_box, width=8, height=8,
                                    bg='#18181C', highlightthickness=0)
        self.status_dot.pack(side=tk.LEFT, padx=(0, 5))
        self._draw_dot(TEXT_DIM)
        self.status_label = tk.Label(status_box, text="Ready",
                                     font=self.font_stat, bg='#18181C',
                                     fg=TEXT_DIM)
        self.status_label.pack(side=tk.LEFT)

    def _check_db_connection(self):
        """Check if MySQL DB is reachable and update the indicator."""
        try:
            if db_pool is None:
                raise Exception("No pool")
            conn = db_pool.get_connection()
            conn.close()
            self.db_dot.delete("all")
            self.db_dot.create_oval(1, 1, 7, 7, fill=ACCENT_GREEN, outline=ACCENT_GREEN)
        except Exception:
            self.db_dot.delete("all")
            self.db_dot.create_oval(1, 1, 7, 7, fill=ACCENT_RED, outline=ACCENT_RED)

    def _draw_dot(self, color):
        self.status_dot.delete("all")
        self.status_dot.create_oval(1, 1, 9, 9, fill=color, outline=color)

    # ── Body ─────────────────────────────────────────────────────────────
    def _build_body(self):
        body = tk.Frame(self.root, bg=BG_GRAY)
        body.pack(fill=tk.BOTH, expand=True, padx=20, pady=12)
        body.grid_columnconfigure(0, weight=1)
        body.grid_rowconfigure(3, weight=1)  # console expands

        self._build_input_card(body, row=0)
        self._build_metrics_bar(body, row=1)
        self._build_progress_workers_row(body, row=2)
        self._build_console_card(body, row=3)
        self._build_buttons(body, row=4)

        # Footer lives outside the grid body so it's always visible at bottom
        self._build_footer()

    # ── Input Card ───────────────────────────────────────────────────────
    def _build_input_card(self, parent, row):
        card = self._card(parent, "Search Parameters", row)
        inner = tk.Frame(card, bg=CARD_BG)
        inner.pack(fill=tk.X, padx=16, pady=(0, 14))
        inner.grid_columnconfigure(1, weight=1)

        # Product name search
        self._field_label(inner, "Product Name", 0)
        name_frame = tk.Frame(inner, bg=CARD_BG)
        name_frame.grid(row=0, column=1, sticky=tk.EW, pady=4)
        self.product_name_entry = self._entry(name_frame, width=40)
        self.product_name_entry.pack(side=tk.LEFT, fill=tk.X, expand=True)
        self._name_hint = tk.Label(
            name_frame, text="  (fetches all pages automatically)",
            bg=CARD_BG, fg=TEXT_SECONDARY,
            font=font.Font(family="Segoe UI", size=8, slant="italic"))
        self._name_hint.pack(side=tk.LEFT, padx=(6, 0))

        # Search term range
        self._field_label(inner, "Search Term Range", 1)
        range_frame = tk.Frame(inner, bg=CARD_BG)
        range_frame.grid(row=1, column=1, sticky=tk.W, pady=4)
        self.search_start_entry = self._entry(range_frame, width=10)
        self.search_start_entry.pack(side=tk.LEFT)
        tk.Label(range_frame, text="  to  ", bg=CARD_BG, fg=TEXT_SECONDARY,
                 font=self.font_label).pack(side=tk.LEFT)
        self.search_end_entry = self._entry(range_frame, width=10)
        self.search_end_entry.pack(side=tk.LEFT)

        # Page range
        self._page_range_label = tk.Label(
            inner, text="Page Range (1–667)", bg=CARD_BG, fg=TEXT_SECONDARY,
            font=self.font_label)
        self._page_range_label.grid(row=2, column=0, sticky=tk.W,
                                     padx=(0, 12), pady=4)
        page_frame = tk.Frame(inner, bg=CARD_BG)
        page_frame.grid(row=2, column=1, sticky=tk.W, pady=4)
        self.page_start_spin = tk.Spinbox(page_frame, from_=1, to=667, width=6,
                                          font=self.font_entry, relief=tk.FLAT,
                                          bg=INPUT_BG, fg=TEXT_PRIMARY,
                                          disabledbackground='#141418',
                                          disabledforeground=TEXT_DIM,
                                          highlightbackground=INPUT_BORDER,
                                          highlightthickness=1,
                                          highlightcolor=ACCENT_GOLD,
                                          buttonbackground='#28282F',
                                          selectbackground='#333340',
                                          selectforeground=WHITE,
                                          insertbackground=TEXT_PRIMARY)
        self.page_start_spin.pack(side=tk.LEFT)
        self.page_start_spin.delete(0, tk.END)
        self.page_start_spin.insert(0, "1")
        tk.Label(page_frame, text="  to  ", bg=CARD_BG, fg=TEXT_SECONDARY,
                 font=self.font_label).pack(side=tk.LEFT)
        self.page_end_spin = tk.Spinbox(page_frame, from_=1, to=667, width=6,
                                        font=self.font_entry, relief=tk.FLAT,
                                        bg=INPUT_BG, fg=TEXT_PRIMARY,
                                        disabledbackground='#141418',
                                        disabledforeground=TEXT_DIM,
                                        highlightbackground=INPUT_BORDER,
                                        highlightthickness=1,
                                        highlightcolor=ACCENT_GOLD,
                                        buttonbackground='#28282F',
                                        selectbackground='#333340',
                                        selectforeground=WHITE,
                                        insertbackground=TEXT_PRIMARY)
        self.page_end_spin.pack(side=tk.LEFT)
        self.page_end_spin.delete(0, tk.END)
        self.page_end_spin.insert(0, "5")

        # Toggle: disable range + page fields when product name is filled
        def _on_name_change(*_):
            has_name = bool(self.product_name_entry.get().strip())
            state = 'disabled' if has_name else 'normal'
            self.search_start_entry.config(state=state)
            self.search_end_entry.config(state=state)
            self.page_start_spin.config(state=state)
            self.page_end_spin.config(state=state)
            self._page_range_label.config(
                fg=TEXT_DIM if has_name else TEXT_SECONDARY)

        self._name_sv = tk.StringVar()
        self.product_name_entry.config(textvariable=self._name_sv)
        self._name_sv.trace_add('write', _on_name_change)

    # ── Metrics Dashboard ──────────────────────────────────────────────
    def _build_metrics_bar(self, parent, row):
        bar = tk.Frame(parent, bg=BG_GRAY)
        bar.grid(row=row, column=0, sticky=tk.EW, pady=(0, 10))
        bar.grid_columnconfigure((0, 1, 2, 3), weight=1)

        metrics = [
            ("0", "SCRAPED",  ACCENT_GOLD),
            ("0", "SAVED",    ACCENT_GREEN),
            ("0", "ERRORS",   ACCENT_RED),
            ("0", "WORKERS",  ACCENT_BLUE),
        ]
        self.metric_labels = {}
        for i, (val, label, color) in enumerate(metrics):
            cell = tk.Frame(bar, bg=CARD_BG, highlightbackground=BORDER_SUBTLE,
                            highlightthickness=1)
            cell.grid(row=0, column=i, sticky=tk.NSEW,
                      padx=(0 if i == 0 else 5, 0), ipady=10)

            # Thin color accent line on top
            tk.Frame(cell, bg=color, height=2).pack(fill=tk.X, side=tk.TOP)

            v = tk.Label(cell, text=val, bg=CARD_BG, fg=color,
                         font=self.font_metric_value)
            v.pack(pady=(8, 0))
            lbl = tk.Label(cell, text=label, bg=CARD_BG, fg=TEXT_DIM,
                           font=self.font_metric_label)
            lbl.pack(pady=(2, 6))
            self.metric_labels[label.lower()] = v

            # Make ERRORS card clickable
            if label == "ERRORS":
                for widget in (cell, v, lbl):
                    widget.configure(cursor="hand2")
                    widget.bind("<Button-1>", lambda e: self._open_error_window())

        # Set initial worker count
        self.metric_labels['workers'].config(text=str(WORKER_COUNT))

    # ── Progress + Workers (side by side) ────────────────────────────────
    def _build_progress_workers_row(self, parent, row):
        container = tk.Frame(parent, bg=BG_GRAY)
        container.grid(row=row, column=0, sticky=tk.EW, pady=(0, 10))
        container.grid_columnconfigure(0, weight=1)
        container.grid_columnconfigure(1, weight=1)

        # ── Left: Progress card ──────────────────────────────────────
        prog_card = tk.Frame(container, bg=CARD_BG, highlightbackground=BORDER_SUBTLE,
                             highlightthickness=1)
        prog_card.grid(row=0, column=0, sticky=tk.NSEW, padx=(0, 4))

        tk.Label(prog_card, text="Progress", bg=CARD_BG,
                 font=self.font_heading, fg=ACCENT_GOLD).pack(
            anchor=tk.W, padx=14, pady=(10, 6))

        inner = tk.Frame(prog_card, bg=CARD_BG)
        inner.pack(fill=tk.X, padx=14, pady=(0, 10))

        # Page progress
        pg_frame = tk.Frame(inner, bg=CARD_BG)
        pg_frame.pack(fill=tk.X, pady=(0, 4))
        self.page_label = tk.Label(pg_frame, text="Pages: 0 / 0",
                                   bg=CARD_BG, fg=TEXT_SECONDARY, font=self.font_stat)
        self.page_label.pack(anchor=tk.W)
        self.page_progress = ttk.Progressbar(pg_frame, orient=tk.HORIZONTAL,
                                             mode='determinate',
                                             style='Horizontal.TProgressbar')
        self.page_progress.pack(fill=tk.X, pady=(2, 0))

        # Current product
        self.current_product_label = tk.Label(
            inner, text="", bg=CARD_BG, fg=ACCENT_GOLD,
            font=font.Font(family="Segoe UI", size=8, slant="italic"),
            anchor=tk.W)
        self.current_product_label.pack(fill=tk.X, pady=(2, 4))

        # Product progress
        pr_frame = tk.Frame(inner, bg=CARD_BG)
        pr_frame.pack(fill=tk.X, pady=(0, 4))
        self.product_label = tk.Label(pr_frame, text="Products: 0",
                                      bg=CARD_BG, fg=TEXT_SECONDARY,
                                      font=self.font_stat)
        self.product_label.pack(anchor=tk.W)
        self.product_progress = ttk.Progressbar(pr_frame, orient=tk.HORIZONTAL,
                                                mode='determinate',
                                                style='Horizontal.TProgressbar')
        self.product_progress.pack(fill=tk.X, pady=(2, 0))

        # Time row
        time_row = tk.Frame(inner, bg=CARD_BG)
        time_row.pack(fill=tk.X, pady=(4, 0))
        self.start_time_label = tk.Label(time_row, text="Start: --:--:--",
                                         bg=CARD_BG, fg=TEXT_SECONDARY, font=self.font_stat)
        self.start_time_label.pack(side=tk.LEFT)
        self.end_time_label = tk.Label(time_row, text="End: --:--:--",
                                       bg=CARD_BG, fg=TEXT_SECONDARY, font=self.font_stat)
        self.end_time_label.pack(side=tk.RIGHT)

        # Stats row
        stats = tk.Frame(inner, bg=CARD_BG)
        stats.pack(fill=tk.X, pady=(4, 0))
        self.time_label = tk.Label(stats, text="Elapsed: 00:00:00",
                                   bg=CARD_BG, fg=TEXT_SECONDARY, font=self.font_stat)
        self.time_label.pack(side=tk.LEFT)
        self.rate_label = tk.Label(stats, text="Rate: 0 products/min",
                                   bg=CARD_BG, fg=TEXT_SECONDARY, font=self.font_stat)
        self.rate_label.pack(side=tk.RIGHT)

        # ── Right: Workers card ──────────────────────────────────────
        work_card = tk.Frame(container, bg=CARD_BG, highlightbackground=BORDER_SUBTLE,
                             highlightthickness=1)
        work_card.grid(row=0, column=1, sticky=tk.NSEW, padx=(4, 0))

        tk.Label(work_card, text="Workers", bg=CARD_BG,
                 font=self.font_heading, fg=ACCENT_GOLD).pack(
            anchor=tk.W, padx=14, pady=(10, 6))

        w_inner = tk.Frame(work_card, bg=CARD_BG)
        w_inner.pack(fill=tk.BOTH, expand=True, padx=14, pady=(0, 10))

        self.worker_slots = []

        _slot_bg = '#141418'
        for i in range(WORKER_COUNT):
            slot = tk.Frame(w_inner, bg=_slot_bg, highlightbackground=BORDER_SUBTLE,
                            highlightthickness=1)
            slot.pack(fill=tk.X, pady=1, ipady=3)

            # Status dot
            dot_canvas = tk.Canvas(slot, width=8, height=8, bg=_slot_bg,
                                   highlightthickness=0)
            dot_canvas.pack(side=tk.LEFT, padx=(10, 6))
            dot_id = dot_canvas.create_oval(1, 1, 7, 7, fill='#333340',
                                            outline='#333340')

            # Worker name
            name_lbl = tk.Label(
                slot, text=f"W{i + 1}", bg=_slot_bg, fg=TEXT_DIM,
                font=font.Font(family="Segoe UI Semibold", size=8),
                width=3, anchor=tk.W)
            name_lbl.pack(side=tk.LEFT)

            # Separator
            tk.Frame(slot, bg=BORDER_SUBTLE, width=1).pack(
                side=tk.LEFT, fill=tk.Y, padx=4, pady=2)

            # Product label
            prod_lbl = tk.Label(
                slot, text="Idle", bg=_slot_bg, fg=TEXT_DIM,
                font=font.Font(family="Cascadia Mono", size=8),
                anchor=tk.W)
            prod_lbl.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(4, 10))

            self.worker_slots.append({
                'frame': slot,
                'canvas': dot_canvas,
                'dot': dot_id,
                'name': name_lbl,
                'product': prod_lbl,
            })

    def _update_worker(self, worker_idx, product_name=None, state='idle'):
        """Update a worker slot display. state: 'idle', 'working', 'done', 'error'"""
        if worker_idx >= len(self.worker_slots):
            return
        slot = self.worker_slots[worker_idx]

        colors = {
            'idle':    ('#333340', TEXT_DIM),  # dot, text
            'working': (ACCENT_GOLD, TEXT_PRIMARY),
            'done':    (ACCENT_GREEN, ACCENT_GREEN),
            'error':   (ACCENT_RED, ACCENT_RED),
        }
        dot_color, text_color = colors.get(state, colors['idle'])

        def _apply():
            slot['canvas'].itemconfig(slot['dot'], fill=dot_color,
                                      outline=dot_color)
            if product_name:
                slot['product'].config(text=product_name, fg=text_color)
            elif state == 'idle':
                slot['product'].config(text="Idle", fg=TEXT_DIM)

        self.root.after(0, _apply)

    def _reset_workers(self):
        """Reset all worker slots to idle."""
        for i in range(len(self.worker_slots)):
            self._update_worker(i, state='idle')

    # ── Console Card ─────────────────────────────────────────────────────
    def _build_console_card(self, parent, row):
        card = self._card(parent, "Live Output", row, expand=True)
        self.console = scrolledtext.ScrolledText(
            card, height=12, state='disabled', wrap=tk.WORD,
            font=self.font_console, bg=CONSOLE_BG, fg=CONSOLE_FG,
            insertbackground=CONSOLE_FG, relief=tk.FLAT, bd=0,
            padx=12, pady=10)
        self.console.pack(fill=tk.BOTH, expand=True, padx=2, pady=(0, 2))

    # ── Buttons ──────────────────────────────────────────────────────────
    def _build_buttons(self, parent, row):
        bar = tk.Frame(parent, bg=BG_GRAY)
        bar.grid(row=row, column=0, sticky=tk.EW, pady=(10, 0))

        self.start_btn = ttk.Button(bar, text="Start Scraping",
                                    command=self.start_scraping, style='Nav.TButton')
        self.start_btn.pack(side=tk.LEFT, padx=(0, 8))

        self.cancel_btn = ttk.Button(bar, text="Cancel",
                                     command=self.cancel_scraping,
                                     style='Danger.TButton', state='disabled')
        self.cancel_btn.pack(side=tk.LEFT, padx=(0, 8))

        self.retry_btn = ttk.Button(bar, text="Retry Errors",
                                    command=self._open_error_window,
                                    style='Retry.TButton')
        self.retry_btn.pack(side=tk.LEFT)

        ttk.Button(bar, text="Help", command=self.show_help,
                   style='Help.TButton').pack(side=tk.RIGHT)

        ttk.Button(bar, text="View Data", command=self._open_data_viewer,
                   style='Data.TButton').pack(side=tk.RIGHT, padx=(0, 8))

    # ── Footer ───────────────────────────────────────────────────────────
    def _build_footer(self):
        footer = tk.Frame(self.root, bg=BG_GRAY)
        footer.pack(fill=tk.X, side=tk.BOTTOM, padx=24, pady=(0, 10))
        tk.Frame(footer, bg=BORDER_SUBTLE, height=1).pack(fill=tk.X, pady=(0, 8))
        tk.Label(footer, text="Powered by Spiderhunts Technologies Ltd",
                 bg=BG_GRAY, fg=TEXT_DIM,
                 font=font.Font(family="Segoe UI Light", size=8)).pack(anchor=tk.CENTER)

    # ── Helpers ──────────────────────────────────────────────────────────
    def _card(self, parent, title, row, expand=False):
        wrapper = tk.Frame(parent, bg=CARD_BG, highlightbackground=BORDER_SUBTLE,
                           highlightthickness=1)
        wrapper.grid(row=row, column=0, sticky=tk.NSEW if expand else tk.EW,
                     pady=(0, 8))
        if expand:
            parent.grid_rowconfigure(row, weight=1)

        tk.Label(wrapper, text=title, font=self.font_heading,
                 bg=CARD_BG, fg=ACCENT_GOLD, anchor=tk.W).pack(
            fill=tk.X, padx=16, pady=(14, 6))

        sep = tk.Frame(wrapper, bg=BORDER_SUBTLE, height=1)
        sep.pack(fill=tk.X, padx=16)

        spacer = tk.Frame(wrapper, bg=CARD_BG, height=8)
        spacer.pack(fill=tk.X)
        return wrapper

    def _field_label(self, parent, text, row):
        tk.Label(parent, text=text, bg=CARD_BG, fg=TEXT_SECONDARY,
                 font=self.font_label).grid(row=row, column=0,
                                            sticky=tk.W, padx=(0, 12), pady=4)

    def _entry(self, parent, width=20):
        e = tk.Entry(parent, width=width, font=self.font_entry,
                     relief=tk.FLAT, bg=INPUT_BG, fg=TEXT_PRIMARY,
                     disabledbackground='#141418',
                     disabledforeground=TEXT_DIM,
                     highlightbackground=INPUT_BORDER, highlightthickness=1,
                     highlightcolor=ACCENT_GOLD, insertbackground=TEXT_PRIMARY,
                     selectbackground='#333340', selectforeground=WHITE)
        return e

    # ── Reusable Loading Overlay ─────────────────────────────────────
    def _create_loader(self, parent):
        """Create a dark overlay with animated loading indicator.
        Returns (overlay_frame, status_label, animate_id_holder).
        Call overlay.place_forget() to hide, overlay.place(...) to show."""
        _ov_bg = '#0C0C10'
        overlay = tk.Frame(parent, bg=_ov_bg)
        # Inner content centered
        inner = tk.Frame(overlay, bg=_ov_bg)
        inner.place(relx=0.5, rely=0.5, anchor=tk.CENTER)
        # Gold spinner dots (animated via text)
        spinner = tk.Label(inner, text="", bg=_ov_bg, fg=ACCENT_GOLD,
                           font=font.Font(family="Segoe UI", size=18, weight="bold"))
        spinner.pack()
        status = tk.Label(inner, text="Loading...", bg=_ov_bg, fg=TEXT_DIM,
                          font=font.Font(family="Segoe UI", size=9))
        status.pack(pady=(8, 0))
        # Animation state holder
        anim = {'id': None, 'step': 0}

        def animate():
            dots = [".", "..", "...", "....", "...", ".."]
            spinner.config(text=dots[anim['step'] % len(dots)])
            anim['step'] += 1
            anim['id'] = parent.after(300, animate)

        overlay._start_anim = lambda: animate()
        overlay._stop_anim = lambda: (
            parent.after_cancel(anim['id']) if anim['id'] else None)
        overlay._status = status
        return overlay

    def _show_loader(self, overlay, text="Loading..."):
        """Show a loading overlay on its parent."""
        overlay._status.config(text=text)
        overlay.place(relx=0, rely=0, relwidth=1, relheight=1)
        overlay.lift()
        overlay._start_anim()
        overlay.winfo_toplevel().update_idletasks()

    def _hide_loader(self, overlay):
        """Hide a loading overlay."""
        try:
            overlay._stop_anim()
        except Exception:
            pass
        overlay.place_forget()

    def _run_with_loader(self, overlay, task_fn, callback_fn, text="Loading..."):
        """Run task_fn in a background thread showing overlay, then call callback_fn(result) on main thread."""
        self._show_loader(overlay, text)

        def worker():
            try:
                result = task_fn()
                overlay.winfo_toplevel().after(0, lambda: _done(result, None))
            except Exception as e:
                overlay.winfo_toplevel().after(0, lambda: _done(None, e))

        def _done(result, error):
            self._hide_loader(overlay)
            if error:
                messagebox.showerror("Error", str(error))
            else:
                callback_fn(result)

        threading.Thread(target=worker, daemon=True).start()

    def _suppress_logs(self):
        os.environ['WDM_LOG_LEVEL'] = '0'
        os.environ['WDM_PRINT_FIRST_LINE'] = 'False'
        from selenium.webdriver.remote.remote_connection import LOGGER
        import logging
        LOGGER.setLevel(logging.WARNING)

    # ── Logging / Progress ───────────────────────────────────────────────
    def log_message(self, message, level='info'):
        self.console.config(state='normal')
        ts = datetime.now().strftime("%H:%M:%S")
        self.console.insert(tk.END, f"[{ts}] ", 'timestamp')
        self.console.insert(tk.END, f"{message}\n", level)
        self.console.see(tk.END)
        self.console.config(state='disabled')
        self.root.update_idletasks()

    def update_page_progress(self, current, total):
        self.page_progress['maximum'] = total
        self.page_progress['value'] = current
        self.page_label.config(text=f"Pages: {current} / {total}")
        self._update_status_dot()
        self.root.update_idletasks()

    def update_product_progress(self, count):
        self.product_progress['value'] = count
        self.product_label.config(text=f"Products: {count}")
        self.processed_count = count
        self._update_status_dot()
        self._update_stats()
        self.root.update_idletasks()

    def update_current_product(self, name):
        display = name[:80] + ("..." if len(name) > 80 else "")
        self.current_product_label.config(text=display)
        self.root.update_idletasks()

    def _update_metrics(self):
        """Update the metrics dashboard cards."""
        self.error_count = len(self.failed_products)
        self.metric_labels['scraped'].config(text=str(self.processed_count))
        self.metric_labels['saved'].config(text=str(self.saved_count))
        self.metric_labels['errors'].config(text=str(self.error_count))
        self.root.update_idletasks()

    def _update_status_dot(self):
        if self.is_scraping:
            self._draw_dot(ACCENT_GOLD)
            self.status_label.config(text="Working", fg=ACCENT_GOLD)
        else:
            self._draw_dot(TEXT_DIM)
            self.status_label.config(text="Ready", fg=TEXT_DIM)

    def _update_stats(self):
        if not self.start_time:
            return
        elapsed = datetime.now() - self.start_time
        self.time_label.config(text=f"Elapsed: {str(elapsed).split('.')[0]}")
        mins = elapsed.total_seconds() / 60
        rate = self.processed_count / mins if mins > 0 else 0
        self.rate_label.config(text=f"Rate: {rate:.1f} products/min")

    def _update_stats_timer(self):
        if self.is_scraping:
            self._update_stats()
            self.root.after(1000, self._update_stats_timer)

    # ── Scraping ─────────────────────────────────────────────────────────
    def start_scraping(self):
        try:
            product_name = self.product_name_entry.get().strip()
            s_start = self.search_start_entry.get().strip()
            s_end = self.search_end_entry.get().strip()

            # Determine search terms: product name OR numeric range
            if product_name:
                search_terms = [product_name]
                # Product name mode: auto-discover all pages from API
                p_start = None
                p_end = None
            else:
                if not all([s_start, s_end]):
                    messagebox.showerror("Error",
                        "Enter a Product Name or fill the Search Term Range")
                    return
                try:
                    s_start, s_end = int(s_start), int(s_end)
                except ValueError:
                    messagebox.showerror("Error",
                        "Search term range must be numbers")
                    return
                if s_start > s_end:
                    messagebox.showerror("Error",
                        "Start search term must be <= end")
                    return
                search_terms = list(range(s_start, s_end + 1))
                p_start = int(self.page_start_spin.get())
                p_end = int(self.page_end_spin.get())
                if p_start > p_end:
                    messagebox.showerror("Error",
                        "Start page must be <= end page")
                    return

            self.is_scraping = True
            self.should_stop = False
            self.start_btn.config(state='disabled')
            self.cancel_btn.config(state='normal')
            self.start_time = datetime.now()
            self.processed_count = 0
            self.saved_count = 0
            self.error_count = 0
            self.failed_products = []
            self._update_metrics()
            self.start_time_label.config(
                text=f"Start: {self.start_time.strftime('%H:%M:%S')}")
            self.end_time_label.config(text="End: --:--:--")

            self._reset_workers()
            # Reinitialize worker slot queue

            self._worker_slots_q = queue.Queue()
            for i in range(WORKER_COUNT):
                self._worker_slots_q.put(i)
            self._thread_to_slot = {}
            self.page_progress['value'] = 0
            self.product_progress['value'] = 0
            self.product_progress['maximum'] = 0
            self.current_product_label.config(text="")
            self.console.config(state='normal')
            self.console.delete(1.0, tk.END)
            self.console.config(state='disabled')
            self._update_status_dot()

            thread = threading.Thread(
                target=self._run_scraper,
                args=(search_terms, p_start, p_end), daemon=True)
            thread.start()
            self._update_stats_timer()

        except Exception as e:
            self.log_message(f"Error starting scrape: {e}", 'error')
            messagebox.showerror("Error", f"Failed to start: {e}")

    def _process_single_product(self, ref, search_term):
        """Worker function: scrape one product and save to DB. Runs in thread pool."""
        if self.should_stop:
            return None

        # Claim a worker slot for UI tracking
        tid = threading.current_thread().ident
        slot_idx = None
        try:
            slot_idx = self._worker_slots_q.get_nowait()
        except Exception:
            # All slots taken — find or reuse
            slot_idx = self._thread_to_slot.get(tid)

        if slot_idx is not None:
            self._thread_to_slot[tid] = slot_idx
            self._update_worker(slot_idx, ref['mann_filter'], 'working')

        product_data = process_url(
            ref['id'], ref['external_number'],
            ref['manufacturer'], ref['mann_filter'],
            ref['status'], ref['filter_type'], ref['url'])

        result = None
        if product_data:
            try:
                db_id = save_to_db(product_data, search_term)
                result = ('success', ref['mann_filter'], db_id)
            except Exception as db_err:
                result = ('db_error', ref['mann_filter'], str(db_err))
        else:
            result = ('no_data', ref['mann_filter'], None)

        # Update worker slot with result status, then release
        if slot_idx is not None:
            state = 'done' if result and result[0] == 'success' else 'error'
            self._update_worker(
                slot_idx,
                f"{ref['mann_filter']}  ✓" if state == 'done'
                else f"{ref['mann_filter']}  ✗",
                state)
            # Release slot after brief flash so the user sees the result
            self._thread_to_slot.pop(tid, None)
            try:
                self._worker_slots_q.put_nowait(slot_idx)
            except Exception:
                pass

        return result

    def _fetch_page_api(self, search_term, page):
        """Fetch product list from API for a single page. Runs in page thread pool."""
        try:
            response = requests.get(BASE_URL, params={
                "query": GRAPHQL_QUERY,
                "variables": json.dumps({
                    "search": str(search_term),
                    "currentPage": page,
                    "pageSize": 15,
                    "filterBy": "ALL_FILTER"
                })
            }, timeout=30)
            response.raise_for_status()
            data = response.json()
            if 'errors' in data:
                return None, page
            return data['data']['catalogSearch'], page
        except Exception as e:
            return None, page

    def _run_scraper(self, search_terms, page_start, page_end):
        cleanup_drivers()
        product_executor = ThreadPoolExecutor(max_workers=WORKER_COUNT)
        page_executor = ThreadPoolExecutor(max_workers=PAGE_CONCURRENCY)
        try:
            auto_pages = page_start is None  # product-name mode
            self.log_message(
                f"Starting with {WORKER_COUNT} workers, "
                f"{PAGE_CONCURRENCY} pages in parallel", 'info')

            for search_term in search_terms:
                if self.should_stop:
                    break

                product_id = 0
                pages_completed = 0

                self.log_message(f"\nProcessing search term: {search_term}", 'info')

                if auto_pages:
                    # Fetch page 1 first to discover total pages
                    self.log_message("Fetching page 1 to discover total pages...",
                                     'info')
                    catalog, _ = self._fetch_page_api(search_term, 1)
                    if catalog is None:
                        self.log_message("API returned no results.", 'warning')
                        continue
                    page_info = catalog.get('pageInfo', {})
                    discovered_total = page_info.get('totalPages', 1)
                    total_items = catalog.get('totalCount', 0)
                    self.log_message(
                        f"Found {total_items} items across "
                        f"{discovered_total} pages", 'info')
                    self.product_progress['maximum'] = total_items
                    self.update_page_progress(0, discovered_total)

                    # Collect products from page 1
                    first_page_items = []
                    for item in catalog['items']:
                        if item.get('product'):
                            product_id += 1
                            first_page_items.append({
                                'id': product_id,
                                'external_number': item.get('externalNumber', ''),
                                'manufacturer': item.get('manufacturer', ''),
                                'mann_filter': item['product'].get('sku', ''),
                                'status': extract_product_status(
                                    item['product'].get('attributes', [])),
                                'filter_type': item.get('filterBy', ''),
                                'url': (
                                    "https://www.mann-filter.com/tr-tr/katalog/"
                                    "arama-sonuclar%C4%B1/urun.html/"
                                    f"{item['product'].get('sku', '').lower()}.html"
                                ),
                            })
                    pages_completed = 1
                    self.update_page_progress(1, discovered_total)

                    # Process page-1 products
                    if first_page_items and not self.should_stop:
                        self.log_message(
                            f"Processing {len(first_page_items)} products "
                            f"from page 1...", 'info')
                        futs = {
                            product_executor.submit(
                                self._process_single_product, ref, search_term
                            ): ref for ref in first_page_items
                        }
                        for future in as_completed(futs):
                            if self.should_stop:
                                break
                            ref = futs[future]
                            try:
                                result = future.result()
                                if result is None:
                                    continue
                                status, name, detail = result
                                if status == 'success':
                                    self.saved_count += 1
                                    self.log_message(
                                        f"Saved to DB (id={detail}): {name}",
                                        'success')
                                elif status == 'db_error':
                                    self.error_count += 1
                                    self.failed_products.append({
                                        'ref': ref, 'search_term': search_term,
                                        'error': f"DB: {detail}",
                                        'time': datetime.now().strftime('%H:%M:%S')})
                                    self.log_message(
                                        f"DB error for {name}: {detail}", 'error')
                                elif status == 'no_data':
                                    self.error_count += 1
                                    self.failed_products.append({
                                        'ref': ref, 'search_term': search_term,
                                        'error': "No data returned from page",
                                        'time': datetime.now().strftime('%H:%M:%S')})
                            except Exception as exc:
                                self.error_count += 1
                                self.failed_products.append({
                                    'ref': ref, 'search_term': search_term,
                                    'error': str(exc),
                                    'time': datetime.now().strftime('%H:%M:%S')})
                                self.log_message(
                                    f"Worker error for {ref['mann_filter']}: "
                                    f"{exc}", 'error')
                            self.processed_count += 1
                            self.update_product_progress(self.processed_count)
                            self.update_current_product(ref['mann_filter'])
                            self._update_metrics()

                    # Use remaining pages (2..N) for the normal batch loop
                    eff_start = 2
                    eff_end = discovered_total
                else:
                    eff_start = page_start
                    eff_end = page_end

                self.update_page_progress(pages_completed,
                                          eff_end - (1 if auto_pages else eff_start) + 1)

                # Process pages in batches of PAGE_CONCURRENCY
                all_pages = list(range(eff_start, eff_end + 1))
                total_items_set = auto_pages  # already set if auto

                for batch_start in range(0, len(all_pages), PAGE_CONCURRENCY):
                    if self.should_stop:
                        break

                    batch = all_pages[batch_start:batch_start + PAGE_CONCURRENCY]
                    self.log_message(
                        f"Fetching pages {batch[0]}–{batch[-1]} simultaneously "
                        f"({len(batch)} pages)...", 'info')

                    # ── Fetch all pages in this batch concurrently ────────
                    page_futures = {
                        page_executor.submit(self._fetch_page_api, search_term, p): p
                        for p in batch
                    }

                    # Collect products from all pages in this batch
                    batch_items = []
                    for pf in as_completed(page_futures):
                        if self.should_stop:
                            break
                        catalog, page_num = pf.result()
                        if catalog is None:
                            self.log_message(
                                f"API error on page {page_num}", 'error')
                            self.error_count += 1
                            self._update_metrics()
                            continue

                        # Set total items from first successful response
                        if not total_items_set:
                            total_items = catalog.get('totalCount', 0)
                            self.log_message(
                                f"Found {total_items} total items", 'info')
                            self.product_progress['maximum'] = total_items
                            total_items_set = True

                        for item in catalog['items']:
                            if item.get('product'):
                                product_id += 1
                                batch_items.append({
                                    'id': product_id,
                                    'external_number': item.get('externalNumber', ''),
                                    'manufacturer': item.get('manufacturer', ''),
                                    'mann_filter': item['product'].get('sku', ''),
                                    'status': extract_product_status(
                                        item['product'].get('attributes', [])),
                                    'filter_type': item.get('filterBy', ''),
                                    'url': (
                                        "https://www.mann-filter.com/tr-tr/katalog/"
                                        "arama-sonuclar%C4%B1/urun.html/"
                                        f"{item['product'].get('sku', '').lower()}.html"
                                    ),
                                })

                        pages_completed += 1
                        self.update_page_progress(
                            pages_completed,
                            eff_end - (1 if auto_pages else eff_start) + 1)

                    if self.should_stop:
                        break

                    self.log_message(
                        f"Processing {len(batch_items)} products from "
                        f"{len(batch)} pages ({WORKER_COUNT} workers)...", 'info')

                    # ── Submit all products from this batch to workers ────
                    product_futures = {
                        product_executor.submit(
                            self._process_single_product, ref, search_term): ref
                        for ref in batch_items
                    }

                    for future in as_completed(product_futures):
                        if self.should_stop:
                            break
                        ref = product_futures[future]
                        try:
                            result = future.result()
                            if result is None:
                                continue
                            status, name, detail = result
                            if status == 'success':
                                self.saved_count += 1
                                self.log_message(
                                    f"Saved to DB (id={detail}): {name}", 'success')
                            elif status == 'db_error':
                                self.error_count += 1
                                self.failed_products.append({
                                    'ref': ref, 'search_term': search_term,
                                    'error': f"DB: {detail}",
                                    'time': datetime.now().strftime('%H:%M:%S')})
                                self.log_message(
                                    f"DB error for {name}: {detail}", 'error')
                            elif status == 'no_data':
                                self.error_count += 1
                                self.failed_products.append({
                                    'ref': ref, 'search_term': search_term,
                                    'error': "No data returned from page",
                                    'time': datetime.now().strftime('%H:%M:%S')})
                        except Exception as exc:
                            self.error_count += 1
                            self.failed_products.append({
                                'ref': ref, 'search_term': search_term,
                                'error': str(exc),
                                'time': datetime.now().strftime('%H:%M:%S')})
                            self.log_message(
                                f"Worker error for {ref['mann_filter']}: {exc}",
                                'error')
                        self.processed_count += 1
                        self.update_product_progress(self.processed_count)
                        self.update_current_product(ref['mann_filter'])
                        self._update_metrics()

                    time.sleep(0.3)

                if not self.should_stop:
                    self.log_message(
                        f"Completed search term {search_term}", 'success')

            if self.should_stop:
                self.log_message("\nScraping cancelled by user", 'warning')
                messagebox.showinfo("Cancelled", "Scraping was cancelled")
            else:
                self.log_message("\nAll search terms processed", 'success')
                messagebox.showinfo("Complete", "Scraping completed!")

        except Exception as e:
            self.log_message(f"\nFatal error: {e}", 'error')
            messagebox.showerror("Error", f"Fatal error: {e}")
        finally:
            # Shut down workers and close their drivers
            product_executor.shutdown(wait=False)
            page_executor.shutdown(wait=False)
            self.log_message("Closing browser workers...", 'info')
            end_time = datetime.now()
            self.root.after(0, lambda: self.end_time_label.config(
                text=f"End: {end_time.strftime('%H:%M:%S')}"))
            self.is_scraping = False
            self.root.after(0, lambda: self.start_btn.config(state='normal'))
            self.root.after(0, lambda: self.cancel_btn.config(state='disabled'))
            self.root.after(0, self._update_status_dot)
            self.root.after(0, self._reset_workers)
            self.log_message("\nScraping finished", 'info')

    # ── Error Window ────────────────────────────────────────────────────
    def _open_error_window(self):
        if not self.failed_products:
            messagebox.showinfo("Errors", "No failed products to show.")
            return

        win = tk.Toplevel(self.root)
        win.title("Failed Products")
        win.geometry("900x600")
        win.configure(bg=BG_GRAY)
        win.minsize(700, 400)

        # Header
        hdr = tk.Frame(win, bg=NAVY, height=44)
        hdr.pack(fill=tk.X)
        hdr.pack_propagate(False)
        tk.Frame(hdr, bg=ACCENT_RED, height=1).pack(fill=tk.X, side=tk.TOP)
        err_title = tk.Label(
            hdr, text=f"Failed Products ({len(self.failed_products)})",
            font=self.font_heading, bg=NAVY, fg=ACCENT_RED)
        err_title.pack(side=tk.LEFT, padx=20, pady=8)

        # Content area (loader overlays this)
        err_content = tk.Frame(win, bg=BG_GRAY)
        err_content.pack(fill=tk.BOTH, expand=True, padx=0, pady=0)

        # Treeview
        tree_frame = tk.Frame(err_content, bg=CARD_BG)
        tree_frame.pack(fill=tk.BOTH, expand=True, padx=12, pady=(12, 0))

        columns = ('idx', 'mann_filter', 'manufacturer', 'error', 'time')
        tree_style = ttk.Style()
        tree_style.configure('Error.Treeview',
                             background=CARD_BG, foreground=TEXT_PRIMARY,
                             fieldbackground=CARD_BG, rowheight=28,
                             font=self.font_stat, borderwidth=0)
        tree_style.configure('Error.Treeview.Heading',
                             background=CARD_BG_ALT, foreground=ACCENT_GOLD,
                             font=self.font_label, borderwidth=0)
        tree_style.map('Error.Treeview',
                       background=[('selected', '#1E2838')],
                       foreground=[('selected', WHITE)])

        tree = ttk.Treeview(tree_frame, columns=columns, show='headings',
                            style='Error.Treeview', selectmode='extended')
        tree.heading('idx', text='#')
        tree.heading('mann_filter', text='MANN Filter')
        tree.heading('manufacturer', text='Manufacturer')
        tree.heading('error', text='Error')
        tree.heading('time', text='Time')
        tree.column('idx', width=40, anchor=tk.CENTER)
        tree.column('mann_filter', width=150)
        tree.column('manufacturer', width=140)
        tree.column('error', width=380)
        tree.column('time', width=70, anchor=tk.CENTER)

        vsb = ttk.Scrollbar(tree_frame, orient=tk.VERTICAL, command=tree.yview)
        tree.configure(yscrollcommand=vsb.set)
        vsb.pack(side=tk.RIGHT, fill=tk.Y)
        tree.pack(fill=tk.BOTH, expand=True)

        for i, fp in enumerate(self.failed_products):
            ref = fp['ref']
            tree.insert('', tk.END, iid=str(i), values=(
                i + 1,
                ref.get('mann_filter', ''),
                ref.get('manufacturer', ''),
                fp.get('error', ''),
                fp.get('time', '')))

        # Detail panel
        detail_frame = tk.Frame(err_content, bg=CARD_BG,
                                highlightbackground=BORDER_SUBTLE,
                                highlightthickness=1)
        detail_frame.pack(fill=tk.X, padx=12, pady=8)
        detail_label = tk.Label(detail_frame, text="Click a row to see details",
                                bg=CARD_BG, fg=TEXT_SECONDARY, font=self.font_stat,
                                anchor=tk.W, wraplength=860, justify=tk.LEFT)
        detail_label.pack(fill=tk.X, padx=12, pady=8)

        def on_select(event):
            sel = tree.selection()
            if not sel:
                return
            idx = int(sel[0])
            if idx >= len(self.failed_products):
                return
            fp = self.failed_products[idx]
            ref = fp['ref']
            detail_label.config(
                text=(f"MANN Filter: {ref.get('mann_filter', '')}  |  "
                      f"External: {ref.get('external_number', '')}  |  "
                      f"Manufacturer: {ref.get('manufacturer', '')}\n"
                      f"URL: {ref.get('url', '')}\n"
                      f"Error: {fp.get('error', '')}"),
                fg=TEXT_PRIMARY)

        tree.bind('<<TreeviewSelect>>', on_select)

        # Loader overlay for retry operations
        err_loader = self._create_loader(err_content)

        # Buttons
        btn_bar = tk.Frame(win, bg=BG_GRAY)
        btn_bar.pack(fill=tk.X, padx=12, pady=(0, 12))

        def retry_selected():
            sel = tree.selection()
            if not sel:
                messagebox.showinfo("Retry", "Select items to retry.")
                return
            indices = sorted([int(s) for s in sel], reverse=True)
            items_to_retry = [self.failed_products[i] for i in indices]
            self._show_loader(err_loader,
                              f"Retrying {len(items_to_retry)} products...")
            threading.Thread(
                target=self._retry_products,
                args=(items_to_retry, win, tree, indices, err_loader, err_title),
                daemon=True).start()

        def retry_all():
            if not self.failed_products:
                return
            items_to_retry = list(self.failed_products)
            indices = list(range(len(self.failed_products) - 1, -1, -1))
            self._show_loader(err_loader,
                              f"Retrying {len(items_to_retry)} products...")
            threading.Thread(
                target=self._retry_products,
                args=(items_to_retry, win, tree, indices, err_loader, err_title),
                daemon=True).start()

        ttk.Button(btn_bar, text="Retry Selected", command=retry_selected,
                   style='Retry.TButton').pack(side=tk.LEFT, padx=(0, 8))
        ttk.Button(btn_bar, text="Retry All", command=retry_all,
                   style='Nav.TButton').pack(side=tk.LEFT, padx=(0, 8))
        ttk.Button(btn_bar, text="Close", command=win.destroy,
                   style='Help.TButton').pack(side=tk.RIGHT)

    def _retry_products(self, items, win, tree, indices,
                        loader=None, title_label=None):
        """Retry a list of failed products in a background thread."""
        self.log_message(f"Retrying {len(items)} failed products...", 'warning')
        executor = ThreadPoolExecutor(max_workers=WORKER_COUNT)
        futures = {}
        for item in items:
            ref = item['ref']
            f = executor.submit(self._process_single_product,
                                ref, item['search_term'])
            futures[f] = item

        succeeded = []
        for future in as_completed(futures):
            item = futures[future]
            ref = item['ref']
            try:
                result = future.result()
                if result and result[0] == 'success':
                    succeeded.append(item)
                    self.saved_count += 1
                    self.error_count = max(0, self.error_count - 1)
                    self.log_message(
                        f"Retry OK (id={result[2]}): {ref['mann_filter']}",
                        'success')
                else:
                    reason = result[2] if result and result[0] == 'db_error' else 'No data'
                    item['error'] = f"Retry failed: {reason}"
                    item['time'] = datetime.now().strftime('%H:%M:%S')
                    self.log_message(
                        f"Retry failed: {ref['mann_filter']}", 'error')
            except Exception as exc:
                item['error'] = f"Retry failed: {exc}"
                item['time'] = datetime.now().strftime('%H:%M:%S')
                self.log_message(
                    f"Retry error: {ref['mann_filter']}: {exc}", 'error')

        executor.shutdown(wait=False)

        # Remove succeeded items from failed_products
        for item in succeeded:
            if item in self.failed_products:
                self.failed_products.remove(item)

        self._update_metrics()
        self.log_message(
            f"Retry done: {len(succeeded)} recovered, "
            f"{len(items) - len(succeeded)} still failed", 'info')

        # Refresh the error window treeview and hide loader
        def _finish():
            if loader:
                self._hide_loader(loader)
            self._refresh_error_tree(tree)
            if title_label:
                title_label.config(
                    text=f"Failed Products ({len(self.failed_products)})")

        try:
            win.after(0, _finish)
        except Exception:
            pass

    def _refresh_error_tree(self, tree):
        """Repopulate the error treeview with current failed_products."""
        tree.delete(*tree.get_children())
        for i, fp in enumerate(self.failed_products):
            ref = fp['ref']
            tree.insert('', tk.END, iid=str(i), values=(
                i + 1,
                ref.get('mann_filter', ''),
                ref.get('manufacturer', ''),
                fp.get('error', ''),
                fp.get('time', '')))

    # ── Data Viewer Window ────────────────────────────────────────────
    def _open_data_viewer(self):
        DV_PAGE_SIZE = 50  # rows per page

        win = tk.Toplevel(self.root)
        win.title("MANN-FILTER — Data Viewer")
        win.geometry("1100x700")
        win.configure(bg=BG_GRAY)
        win.minsize(900, 500)

        # Pagination state
        pag = {'page': 1, 'total_rows': 0, 'total_pages': 1}

        # Header
        hdr = tk.Frame(win, bg=NAVY, height=52)
        hdr.pack(fill=tk.X)
        hdr.pack_propagate(False)
        tk.Frame(hdr, bg=ACCENT_BLUE, height=1).pack(fill=tk.X, side=tk.TOP)
        tk.Label(hdr, text="Data Viewer", font=self.font_heading,
                 bg=NAVY, fg=ACCENT_BLUE).pack(side=tk.LEFT, padx=20, pady=10)
        dv_count_label = tk.Label(hdr, text="", font=self.font_stat,
                                  bg=NAVY, fg=TEXT_SECONDARY)
        dv_count_label.pack(side=tk.RIGHT, padx=16)
        # Placeholder for export buttons (populated after handlers are defined)
        hdr_export_area = tk.Frame(hdr, bg=NAVY)
        hdr_export_area.pack(side=tk.RIGHT, padx=(0, 6))

        # Search bar
        search_frame = tk.Frame(win, bg=CARD_BG, highlightbackground=BORDER_SUBTLE,
                                highlightthickness=1)
        search_frame.pack(fill=tk.X, padx=12, pady=(10, 0))
        tk.Label(search_frame, text="Search:", bg=CARD_BG, fg=TEXT_SECONDARY,
                 font=self.font_label).pack(side=tk.LEFT, padx=(14, 8), pady=8)
        search_var = tk.StringVar()
        search_entry = tk.Entry(search_frame, textvariable=search_var, width=40,
                                font=self.font_entry, relief=tk.FLAT,
                                bg=INPUT_BG, fg=TEXT_PRIMARY,
                                highlightbackground=INPUT_BORDER,
                                highlightthickness=1,
                                highlightcolor=ACCENT_GOLD,
                                insertbackground=TEXT_PRIMARY,
                                selectbackground='#333340',
                                selectforeground=WHITE)
        search_entry.pack(side=tk.LEFT, padx=(0, 8), pady=8)

        # Search filters
        filter_var = tk.StringVar(value="All Fields")
        filter_menu = ttk.Combobox(search_frame, textvariable=filter_var,
                                   values=["All Fields", "MANN Filter",
                                           "Manufacturer", "External Number",
                                           "Status", "Filter Type"],
                                   state='readonly', width=16)
        filter_menu.pack(side=tk.LEFT, padx=(0, 8), pady=8)

        # Content area (holds tree + detail; loader overlays this)
        content = tk.Frame(win, bg=BG_GRAY)
        content.pack(fill=tk.BOTH, expand=True, padx=12, pady=0)

        # Products Treeview
        tree_frame = tk.Frame(content, bg=CARD_BG)
        tree_frame.pack(fill=tk.BOTH, expand=True, pady=(8, 0))

        # ── Checkbox images (crisp pixel-drawn) ──────────────────────
        _sz = 16
        _unchecked_img = tk.PhotoImage(width=_sz, height=_sz)
        _checked_img = tk.PhotoImage(width=_sz, height=_sz)
        # Keep references so GC doesn't destroy them
        win._cb_imgs = (_unchecked_img, _checked_img)

        # Draw unchecked: rounded-look box with border
        _border = '#555564'
        _fill = INPUT_BG
        _row_unc = []
        for y in range(_sz):
            row = []
            for x in range(_sz):
                if (x <= 1 or x >= _sz - 2 or y <= 1 or y >= _sz - 2):
                    row.append(_border)
                else:
                    row.append(_fill)
            _row_unc.append(row)
        # Soften corners
        for cx, cy in [(0, 0), (1, 0), (0, 1),
                        (_sz-1, 0), (_sz-2, 0), (_sz-1, 1),
                        (0, _sz-1), (1, _sz-1), (0, _sz-2),
                        (_sz-1, _sz-1), (_sz-2, _sz-1), (_sz-1, _sz-2)]:
            _row_unc[cy][cx] = _fill
        for y in range(_sz):
            _unchecked_img.put('{' + ' '.join(_row_unc[y]) + '}', to=(0, y))

        # Draw checked: green filled box with white checkmark
        _cbg = ACCENT_GREEN
        _cfg = '#FFFFFF'
        _row_chk = []
        for y in range(_sz):
            row = []
            for x in range(_sz):
                row.append(_cbg)
            _row_chk.append(row)
        # Soften corners
        for cx, cy in [(0, 0), (1, 0), (0, 1),
                        (_sz-1, 0), (_sz-2, 0), (_sz-1, 1),
                        (0, _sz-1), (1, _sz-1), (0, _sz-2),
                        (_sz-1, _sz-1), (_sz-2, _sz-1), (_sz-1, _sz-2)]:
            _row_chk[cy][cx] = CARD_BG
        # Draw checkmark (thick 2px)
        _cm = [(3,8),(4,9),(5,10),(6,11),(7,10),(8,9),(9,8),(10,7),(11,6),(12,5),
               (3,9),(4,10),(5,11),(6,12),(7,11),(8,10),(9,9),(10,8),(11,7),(12,6),
               (4,8),(5,9),(6,10),(7,9),(8,8),(9,7),(10,6),(11,5),(12,4)]
        for cx, cy in _cm:
            if 0 <= cx < _sz and 0 <= cy < _sz:
                _row_chk[cy][cx] = _cfg
        for y in range(_sz):
            _checked_img.put('{' + ' '.join(_row_chk[y]) + '}', to=(0, y))

        dv_style = ttk.Style()
        dv_style.configure('DV.Treeview',
                           background=CARD_BG, foreground=TEXT_PRIMARY,
                           fieldbackground=CARD_BG, rowheight=28,
                           font=self.font_stat, borderwidth=0,
                           indent=0)
        dv_style.configure('DV.Treeview.Heading',
                           background=CARD_BG_ALT, foreground=ACCENT_GOLD,
                           font=self.font_label, borderwidth=0,
                           relief=tk.FLAT)
        dv_style.map('DV.Treeview.Heading',
                     background=[('active', CARD_BG_ALT)],
                     foreground=[('active', ACCENT_GOLD)],
                     relief=[('active', tk.FLAT)])
        dv_style.map('DV.Treeview',
                     background=[('selected', '#1E2838')],
                     foreground=[('selected', WHITE)])

        columns = ('id', 'search_term', 'external_number',
                   'manufacturer', 'mann_filter', 'status', 'filter_type',
                   'created_at')
        tree = ttk.Treeview(tree_frame, columns=columns, show='tree headings',
                            style='DV.Treeview', selectmode='browse')

        # Checkbox column (#0 tree column) with image support
        tree.heading('#0', text='  All', image=_unchecked_img, anchor=tk.W)
        tree.column('#0', width=70, minwidth=70, stretch=False, anchor=tk.CENTER)

        tree.heading('id', text='ID', anchor=tk.CENTER)
        tree.heading('search_term', text='Search', anchor=tk.CENTER)
        tree.heading('external_number', text='External No.', anchor=tk.CENTER)
        tree.heading('manufacturer', text='Manufacturer', anchor=tk.CENTER)
        tree.heading('mann_filter', text='MANN Filter', anchor=tk.CENTER)
        tree.heading('status', text='Status', anchor=tk.CENTER)
        tree.heading('filter_type', text='Filter Type', anchor=tk.CENTER)
        tree.heading('created_at', text='Created', anchor=tk.CENTER)
        tree.column('id', width=50, anchor=tk.CENTER)
        tree.column('search_term', width=60, anchor=tk.CENTER)
        tree.column('external_number', width=130, anchor=tk.CENTER)
        tree.column('manufacturer', width=140, anchor=tk.CENTER)
        tree.column('mann_filter', width=140, anchor=tk.CENTER)
        tree.column('status', width=90, anchor=tk.CENTER)
        tree.column('filter_type', width=120, anchor=tk.CENTER)
        tree.column('created_at', width=140, anchor=tk.CENTER)

        vsb = ttk.Scrollbar(tree_frame, orient=tk.VERTICAL, command=tree.yview)
        hsb = ttk.Scrollbar(tree_frame, orient=tk.HORIZONTAL, command=tree.xview)
        tree.configure(yscrollcommand=vsb.set, xscrollcommand=hsb.set)
        vsb.pack(side=tk.RIGHT, fill=tk.Y)
        hsb.pack(side=tk.BOTTOM, fill=tk.X)
        tree.pack(fill=tk.BOTH, expand=True)

        # ── Checkbox state ──────────────────────────────────────────
        checked_ids = set()  # product IDs that are checked

        def _chk_img(product_id):
            return _checked_img if product_id in checked_ids else _unchecked_img

        def _toggle_row(iid):
            """Toggle checkbox for a single row."""
            pid = int(iid)
            if pid in checked_ids:
                checked_ids.discard(pid)
            else:
                checked_ids.add(pid)
            tree.item(iid, image=_chk_img(pid))
            _update_select_all_header()

        def _toggle_all():
            """Toggle all checkboxes on the current page."""
            all_iids = tree.get_children()
            all_pids = {int(iid) for iid in all_iids}
            if all_pids.issubset(checked_ids):
                checked_ids.difference_update(all_pids)
            else:
                checked_ids.update(all_pids)
            for iid in all_iids:
                tree.item(iid, image=_chk_img(int(iid)))
            _update_select_all_header()

        def _update_select_all_header():
            """Update the header checkbox image."""
            all_iids = tree.get_children()
            if all_iids:
                all_pids = {int(iid) for iid in all_iids}
                if all_pids.issubset(checked_ids):
                    tree.heading('#0', text='  All', image=_checked_img)
                else:
                    tree.heading('#0', text='  All', image=_unchecked_img)
            else:
                tree.heading('#0', text='  All', image=_unchecked_img)

        # Click on checkbox column -> toggle; other columns work normally
        def _on_tree_click(event):
            region = tree.identify_region(event.x, event.y)
            col = tree.identify_column(event.x)
            if col == '#0':  # checkbox tree column
                if region == 'heading':
                    _toggle_all()
                    return 'break'
                elif region in ('cell', 'tree'):
                    iid = tree.identify_row(event.y)
                    if iid:
                        _toggle_row(iid)

        tree.bind('<Button-1>', _on_tree_click, add=True)

        # ── Pagination bar ───────────────────────────────────────────
        pag_frame = tk.Frame(content, bg=CARD_BG, highlightbackground=BORDER_SUBTLE,
                             highlightthickness=1)
        pag_frame.pack(fill=tk.X, pady=(6, 0))

        prev_btn = tk.Button(
            pag_frame, text="\u25C0  Prev", font=self.font_button,
            bg=CARD_BG_ALT, fg=TEXT_PRIMARY, activebackground='#28282F',
            activeforeground=TEXT_PRIMARY, relief=tk.FLAT, padx=14, pady=4,
            cursor='hand2', state='disabled',
            disabledforeground=TEXT_DIM)
        prev_btn.pack(side=tk.LEFT, padx=(8, 4), pady=6)

        next_btn = tk.Button(
            pag_frame, text="Next  \u25B6", font=self.font_button,
            bg=CARD_BG_ALT, fg=TEXT_PRIMARY, activebackground='#28282F',
            activeforeground=TEXT_PRIMARY, relief=tk.FLAT, padx=14, pady=4,
            cursor='hand2', state='disabled',
            disabledforeground=TEXT_DIM)
        next_btn.pack(side=tk.LEFT, padx=(0, 8), pady=6)

        page_label = tk.Label(pag_frame, text="Page 1 of 1",
                              bg=CARD_BG, fg=TEXT_SECONDARY, font=self.font_stat)
        page_label.pack(side=tk.LEFT, padx=8)

        rows_label = tk.Label(pag_frame, text="",
                              bg=CARD_BG, fg=TEXT_DIM, font=self.font_stat)
        rows_label.pack(side=tk.RIGHT, padx=12)

        # Detail panel (shows dims, vehicles, OEM when a row is clicked)
        detail_card = tk.Frame(content, bg=CARD_BG, highlightbackground=BORDER_SUBTLE,
                               highlightthickness=1, height=220)
        detail_card.pack(fill=tk.X, pady=(8, 0))
        detail_card.pack_propagate(False)

        tk.Label(detail_card, text="Product Details", font=self.font_heading,
                 bg=CARD_BG, fg=ACCENT_GOLD).pack(anchor=tk.W, padx=14, pady=(10, 4))
        tk.Frame(detail_card, bg=BORDER_SUBTLE, height=1).pack(fill=tk.X, padx=14)

        detail_nb = ttk.Notebook(detail_card)
        detail_nb.pack(fill=tk.BOTH, expand=True, padx=8, pady=4)

        # ── Dimensions tab (two-column table) ────────────────────────
        dims_frame = tk.Frame(detail_nb, bg=CONSOLE_BG)
        detail_nb.add(dims_frame, text="Dimensions")

        detail_tree_style = ttk.Style()
        detail_tree_style.configure('Detail.Treeview',
                                    background='#141418', foreground=TEXT_PRIMARY,
                                    fieldbackground='#141418', rowheight=26,
                                    font=self.font_console, borderwidth=0)
        detail_tree_style.configure('Detail.Treeview.Heading',
                                    background=CARD_BG_ALT, foreground=ACCENT_GOLD,
                                    font=self.font_label, borderwidth=0)
        detail_tree_style.map('Detail.Treeview',
                              background=[('selected', '#1E2838')],
                              foreground=[('selected', WHITE)])

        dims_tree = ttk.Treeview(dims_frame, columns=('name', 'value'),
                                 show='headings', style='Detail.Treeview')
        dims_tree.heading('name', text='Dimension', anchor=tk.W)
        dims_tree.heading('value', text='Value', anchor=tk.W)
        dims_tree.column('name', width=250, anchor=tk.W)
        dims_tree.column('value', width=300, anchor=tk.W)
        dims_vsb = ttk.Scrollbar(dims_frame, orient=tk.VERTICAL,
                                 command=dims_tree.yview)
        dims_tree.configure(yscrollcommand=dims_vsb.set)
        dims_vsb.pack(side=tk.RIGHT, fill=tk.Y)
        dims_tree.pack(fill=tk.BOTH, expand=True)

        # Empty-state label for dimensions
        dims_empty = tk.Label(dims_frame, text="Click a product to view dimensions",
                              bg=CONSOLE_BG, fg=TEXT_SECONDARY, font=self.font_stat)

        # ── Vehicles tab (multi-column table) ────────────────────────
        veh_frame = tk.Frame(detail_nb, bg=CONSOLE_BG)
        detail_nb.add(veh_frame, text="Vehicles")

        veh_cols = ('brand', 'model', 'model_type', 'filter_type',
                    'engine', 'ccm', 'kw', 'hp', 'year')
        veh_tree = ttk.Treeview(veh_frame, columns=veh_cols,
                                show='headings', style='Detail.Treeview')
        veh_tree.heading('brand', text='Brand', anchor=tk.W)
        veh_tree.heading('model', text='Model', anchor=tk.W)
        veh_tree.heading('model_type', text='Model Type', anchor=tk.W)
        veh_tree.heading('filter_type', text='Filter Type', anchor=tk.W)
        veh_tree.heading('engine', text='Engine', anchor=tk.W)
        veh_tree.heading('ccm', text='ccm', anchor=tk.CENTER)
        veh_tree.heading('kw', text='kW', anchor=tk.CENTER)
        veh_tree.heading('hp', text='HP', anchor=tk.CENTER)
        veh_tree.heading('year', text='Year', anchor=tk.CENTER)
        veh_tree.column('brand', width=110)
        veh_tree.column('model', width=120)
        veh_tree.column('model_type', width=120)
        veh_tree.column('filter_type', width=100)
        veh_tree.column('engine', width=90)
        veh_tree.column('ccm', width=55, anchor=tk.CENTER)
        veh_tree.column('kw', width=50, anchor=tk.CENTER)
        veh_tree.column('hp', width=50, anchor=tk.CENTER)
        veh_tree.column('year', width=90, anchor=tk.CENTER)
        veh_vsb = ttk.Scrollbar(veh_frame, orient=tk.VERTICAL,
                                command=veh_tree.yview)
        veh_hsb = ttk.Scrollbar(veh_frame, orient=tk.HORIZONTAL,
                                command=veh_tree.xview)
        veh_tree.configure(yscrollcommand=veh_vsb.set,
                           xscrollcommand=veh_hsb.set)
        veh_vsb.pack(side=tk.RIGHT, fill=tk.Y)
        veh_hsb.pack(side=tk.BOTTOM, fill=tk.X)
        veh_tree.pack(fill=tk.BOTH, expand=True)

        veh_empty = tk.Label(veh_frame, text="Click a product to view vehicles",
                             bg=CONSOLE_BG, fg=TEXT_SECONDARY, font=self.font_stat)

        # ── OEM Numbers tab (grouped table) ──────────────────────────
        oem_frame = tk.Frame(detail_nb, bg=CONSOLE_BG)
        detail_nb.add(oem_frame, text="OEM Numbers")

        oem_tree = ttk.Treeview(oem_frame, columns=('manufacturer', 'number'),
                                show='headings', style='Detail.Treeview')
        oem_tree.heading('manufacturer', text='Manufacturer', anchor=tk.W)
        oem_tree.heading('number', text='OEM Number', anchor=tk.W)
        oem_tree.column('manufacturer', width=250, anchor=tk.W)
        oem_tree.column('number', width=300, anchor=tk.W)
        oem_vsb = ttk.Scrollbar(oem_frame, orient=tk.VERTICAL,
                                command=oem_tree.yview)
        oem_tree.configure(yscrollcommand=oem_vsb.set)
        oem_vsb.pack(side=tk.RIGHT, fill=tk.Y)
        oem_tree.pack(fill=tk.BOTH, expand=True)

        oem_empty = tk.Label(oem_frame, text="Click a product to view OEM numbers",
                             bg=CONSOLE_BG, fg=TEXT_SECONDARY, font=self.font_stat)

        # Loading overlay for the content area
        loader = self._create_loader(content)
        # Small loader for the detail panel
        detail_loader = self._create_loader(detail_card)

        # ── DB helpers ───────────────────────────────────────────────
        field_map = {
            "All Fields": None,
            "MANN Filter": "mann_filter",
            "Manufacturer": "manufacturer",
            "External Number": "external_number",
            "Status": "status",
            "Filter Type": "filter_type",
        }

        def _build_where(search, field):
            """Return (where_clause, params) for search filtering."""
            if not search:
                return "", ()
            col = field_map.get(field)
            if col:
                return f" WHERE {col} LIKE %s", (f"%{search}%",)
            return (" WHERE mann_filter LIKE %s OR manufacturer LIKE %s OR "
                    "external_number LIKE %s OR status LIKE %s OR "
                    "filter_type LIKE %s",
                    tuple(f"%{search}%" for _ in range(5)))

        def _fetch_page(search, field, page):
            """Fetch one page of rows + total count."""
            conn = db_pool.get_connection()
            cursor = conn.cursor()
            try:
                where, params = _build_where(search, field)
                # Total count
                cursor.execute(f"SELECT COUNT(*) FROM products{where}", params)
                total = cursor.fetchone()[0]
                # Page rows
                offset = (page - 1) * DV_PAGE_SIZE
                cursor.execute(
                    f"SELECT id, search_term, external_number, manufacturer, "
                    f"mann_filter, status, filter_type, created_at "
                    f"FROM products{where} ORDER BY id DESC "
                    f"LIMIT %s OFFSET %s",
                    params + (DV_PAGE_SIZE, offset))
                rows = cursor.fetchall()
                return rows, total
            finally:
                cursor.close()
                conn.close()

        def _update_pagination(total):
            """Update pagination controls after data load."""
            pag['total_rows'] = total
            pag['total_pages'] = max(1, math.ceil(total / DV_PAGE_SIZE))
            # Clamp current page
            if pag['page'] > pag['total_pages']:
                pag['page'] = pag['total_pages']

            page_label.config(
                text=f"Page {pag['page']} of {pag['total_pages']}")
            start_row = (pag['page'] - 1) * DV_PAGE_SIZE + 1
            end_row = min(pag['page'] * DV_PAGE_SIZE, total)
            if total > 0:
                rows_label.config(
                    text=f"Showing {start_row}-{end_row} of {total:,}")
            else:
                rows_label.config(text="No results")
            dv_count_label.config(text=f"{total:,} products")

            prev_btn.config(state='normal' if pag['page'] > 1 else 'disabled')
            next_btn.config(
                state='normal' if pag['page'] < pag['total_pages'] else 'disabled')

        def _populate_tree(result):
            rows, total = result
            tree.delete(*tree.get_children())
            for row in rows:
                pid = row[0]
                tree.insert('', tk.END, iid=str(pid),
                            image=_chk_img(pid),
                            values=tuple(row))
            _update_select_all_header()
            _update_pagination(total)

        def load_page(search="", field="All Fields", page=1):
            pag['page'] = page
            self._run_with_loader(
                loader,
                lambda: _fetch_page(search, field, page),
                _populate_tree,
                text=f"Loading page {page}...")

        # ── Pagination button handlers ───────────────────────────────
        def go_prev():
            if pag['page'] > 1:
                load_page(search_var.get().strip(), filter_var.get(),
                          pag['page'] - 1)

        def go_next():
            if pag['page'] < pag['total_pages']:
                load_page(search_var.get().strip(), filter_var.get(),
                          pag['page'] + 1)

        prev_btn.config(command=go_prev)
        next_btn.config(command=go_next)

        # ── Search with debounce (resets to page 1) ──────────────────
        search_timer = {'id': None}

        def on_search(*_args):
            if search_timer['id']:
                win.after_cancel(search_timer['id'])
            search_timer['id'] = win.after(
                400, lambda: load_page(
                    search_var.get().strip(), filter_var.get(), 1))

        search_var.trace_add('write', on_search)
        filter_menu.bind('<<ComboboxSelected>>', on_search)

        # ── Row detail (with loader on detail panel) ─────────────────
        def _fetch_details(product_id):
            conn = db_pool.get_connection()
            cursor = conn.cursor()
            try:
                cursor.execute(
                    "SELECT dimension_name, dimension_value FROM dimensions "
                    "WHERE product_id = %s", (product_id,))
                dim_rows = cursor.fetchall()
                cursor.execute(
                    "SELECT brand, model, model_type, filter_type, engine_code, "
                    "ccm, kw, hp, year_of_manufacture FROM vehicles "
                    "WHERE product_id = %s", (product_id,))
                veh_rows = cursor.fetchall()
                cursor.execute(
                    "SELECT manufacturer, oem_number FROM oem_numbers "
                    "WHERE product_id = %s", (product_id,))
                oem_rows = cursor.fetchall()
                return dim_rows, veh_rows, oem_rows
            finally:
                cursor.close()
                conn.close()

        def _show_details(data):
            dim_rows, veh_rows, oem_rows = data

            # ── Dimensions ───────────────────────────────────────
            dims_tree.delete(*dims_tree.get_children())
            if dim_rows:
                dims_empty.place_forget()
                for i, (name, val) in enumerate(dim_rows):
                    dims_tree.insert('', tk.END, iid=f'd{i}',
                                     values=(name or '', val or ''))
            else:
                dims_empty.config(text="No dimensions found.")
                dims_empty.place(relx=0.5, rely=0.5, anchor=tk.CENTER)

            # ── Vehicles ─────────────────────────────────────────
            veh_tree.delete(*veh_tree.get_children())
            if veh_rows:
                veh_empty.place_forget()
                for i, v in enumerate(veh_rows):
                    veh_tree.insert('', tk.END, iid=f'v{i}', values=(
                        v[0] or '', v[1] or '', v[2] or '',
                        v[3] or '', v[4] or '', v[5] or '',
                        v[6] or '', v[7] or '', v[8] or ''))
            else:
                veh_empty.config(text="No vehicles found.")
                veh_empty.place(relx=0.5, rely=0.5, anchor=tk.CENTER)

            # ── OEM Numbers ──────────────────────────────────────
            oem_tree.delete(*oem_tree.get_children())
            if oem_rows:
                oem_empty.place_forget()
                for i, (mfr, num) in enumerate(oem_rows):
                    oem_tree.insert('', tk.END, iid=f'o{i}',
                                    values=(mfr or '', num or ''))
            else:
                oem_empty.config(text="No OEM numbers found.")
                oem_empty.place(relx=0.5, rely=0.5, anchor=tk.CENTER)

        def on_row_select(event):
            sel = tree.selection()
            if not sel:
                return
            product_id = int(sel[0])
            self._run_with_loader(
                detail_loader,
                lambda: _fetch_details(product_id),
                _show_details,
                text="Loading details...")

        tree.bind('<<TreeviewSelect>>', on_row_select)

        # ── Export helpers ───────────────────────────────────────────
        PRODUCT_HEADERS = ['ID', 'Search Term', 'External Number',
                           'Manufacturer', 'MANN Filter', 'Status',
                           'Filter Type', 'Created At']
        DIM_HEADERS = ['Dimension', 'Value']
        VEH_HEADERS = ['Brand', 'Model', 'Model Type', 'Filter Type',
                        'Engine Code', 'ccm', 'kW', 'HP', 'Year']
        OEM_HEADERS = ['Manufacturer', 'OEM Number']

        def _fetch_full_product(product_id):
            """Fetch a product row + its details from DB."""
            conn = db_pool.get_connection()
            cursor = conn.cursor()
            try:
                cursor.execute(
                    "SELECT id, search_term, external_number, manufacturer, "
                    "mann_filter, status, filter_type, created_at "
                    "FROM products WHERE id = %s", (product_id,))
                prod = cursor.fetchone()
                cursor.execute(
                    "SELECT dimension_name, dimension_value FROM dimensions "
                    "WHERE product_id = %s", (product_id,))
                dims = cursor.fetchall()
                cursor.execute(
                    "SELECT brand, model, model_type, filter_type, engine_code, "
                    "ccm, kw, hp, year_of_manufacture FROM vehicles "
                    "WHERE product_id = %s", (product_id,))
                vehs = cursor.fetchall()
                cursor.execute(
                    "SELECT manufacturer, oem_number FROM oem_numbers "
                    "WHERE product_id = %s", (product_id,))
                oems = cursor.fetchall()
                return prod, dims, vehs, oems
            finally:
                cursor.close()
                conn.close()

        def _fetch_all_products():
            """Fetch all products (with current search filter) + details."""
            conn = db_pool.get_connection()
            cursor = conn.cursor()
            try:
                where, params = _build_where(
                    search_var.get().strip(), filter_var.get())
                cursor.execute(
                    f"SELECT id, search_term, external_number, manufacturer, "
                    f"mann_filter, status, filter_type, created_at "
                    f"FROM products{where} ORDER BY id DESC", params)
                rows = cursor.fetchall()
                all_data = []
                for row in rows:
                    pid = row[0]
                    cursor.execute(
                        "SELECT dimension_name, dimension_value "
                        "FROM dimensions WHERE product_id = %s", (pid,))
                    dims = cursor.fetchall()
                    cursor.execute(
                        "SELECT brand, model, model_type, filter_type, "
                        "engine_code, ccm, kw, hp, year_of_manufacture "
                        "FROM vehicles WHERE product_id = %s", (pid,))
                    vehs = cursor.fetchall()
                    cursor.execute(
                        "SELECT manufacturer, oem_number FROM oem_numbers "
                        "WHERE product_id = %s", (pid,))
                    oems = cursor.fetchall()
                    all_data.append((row, dims, vehs, oems))
                return all_data
            finally:
                cursor.close()
                conn.close()

        def _safe(val):
            """Convert DB value to clean string for export."""
            if val is None:
                return ''
            s = str(val)
            # datetime objects may have come through as strings already
            return s

        # ── CSV Export ───────────────────────────────────────────────
        def _export_csv(products_data):
            """Export a list of (prod, dims, vehs, oems) tuples to CSV."""
            path = filedialog.asksaveasfilename(
                parent=win, defaultextension='.csv',
                filetypes=[('CSV Files', '*.csv'), ('All Files', '*.*')],
                title='Export as CSV',
                initialfile=f"mann_filter_export_{datetime.now():%Y%m%d_%H%M%S}.csv")
            if not path:
                return

            with open(path, 'w', newline='', encoding='utf-8-sig') as f:
                w = csv.writer(f)

                for prod, dims, vehs, oems in products_data:
                    # Product header
                    w.writerow([])
                    w.writerow(['=== PRODUCT ==='])
                    w.writerow(PRODUCT_HEADERS)
                    w.writerow([_safe(v) for v in prod])

                    # Dimensions
                    if dims:
                        w.writerow([])
                        w.writerow(['--- Dimensions ---'])
                        w.writerow(DIM_HEADERS)
                        for d in dims:
                            w.writerow([_safe(v) for v in d])

                    # Vehicles
                    if vehs:
                        w.writerow([])
                        w.writerow(['--- Vehicles ---'])
                        w.writerow(VEH_HEADERS)
                        for v in vehs:
                            w.writerow([_safe(c) for c in v])

                    # OEM Numbers
                    if oems:
                        w.writerow([])
                        w.writerow(['--- OEM Numbers ---'])
                        w.writerow(OEM_HEADERS)
                        for o in oems:
                            w.writerow([_safe(v) for v in o])

            messagebox.showinfo("Export Complete",
                                f"CSV saved to:\n{path}", parent=win)

        # ── PDF Export ───────────────────────────────────────────────
        def _export_pdf(products_data):
            """Export a list of (prod, dims, vehs, oems) tuples to PDF."""
            if not HAS_FPDF:
                messagebox.showerror(
                    "Missing Library",
                    "PDF export requires the fpdf2 library.\n\n"
                    "Install it with:\n  pip install fpdf2",
                    parent=win)
                return

            path = filedialog.asksaveasfilename(
                parent=win, defaultextension='.pdf',
                filetypes=[('PDF Files', '*.pdf'), ('All Files', '*.*')],
                title='Export as PDF',
                initialfile=f"mann_filter_export_{datetime.now():%Y%m%d_%H%M%S}.pdf")
            if not path:
                return

            pdf = FPDF(orientation='L', unit='mm', format='A4')
            pdf.set_auto_page_break(auto=True, margin=15)

            # Register a Unicode TTF font (Segoe UI on Windows, fallback to Helvetica)
            _use_unicode = False
            for _ttf in ['C:/Windows/Fonts/segoeui.ttf',
                         'C:/Windows/Fonts/arial.ttf']:
                if os.path.exists(_ttf):
                    pdf.add_font('ExportFont', '', _ttf)
                    _use_unicode = True
                    break
            # Also register bold variant
            if _use_unicode:
                for _ttf_b in ['C:/Windows/Fonts/segoeuib.ttf',
                               'C:/Windows/Fonts/arialbd.ttf']:
                    if os.path.exists(_ttf_b):
                        pdf.add_font('ExportFont', 'B', _ttf_b)
                        break

            def _font(style='', size=9):
                if _use_unicode:
                    pdf.set_font('ExportFont', style, size)
                else:
                    pdf.set_font('Helvetica', style, size)

            def _txt(val):
                """Make text safe for PDF output."""
                s = _safe(val)
                if not _use_unicode:
                    s = s.encode('latin-1', 'replace').decode('latin-1')
                return s

            # ── Title page ───────────────────────────────────────
            pdf.add_page()
            pdf.set_fill_color(245, 245, 247)
            pdf.rect(0, 0, 297, 210, 'F')

            pdf.set_text_color(40, 40, 45)
            _font('B', 28)
            pdf.ln(50)
            pdf.cell(0, 15, 'MANN-FILTER', align='C',
                     new_x='LMARGIN', new_y='NEXT')
            _font('', 12)
            pdf.set_text_color(120, 120, 130)
            pdf.cell(0, 10, 'Scraper Pro  -  Data Export', align='C',
                     new_x='LMARGIN', new_y='NEXT')
            pdf.ln(10)
            # Decorative line
            pdf.set_draw_color(200, 168, 76)
            pdf.set_line_width(0.4)
            pdf.line(90, pdf.get_y(), 207, pdf.get_y())
            pdf.ln(8)
            _font('', 9)
            pdf.set_text_color(140, 140, 150)
            pdf.cell(0, 8,
                     f"Generated: {datetime.now():%Y-%m-%d %H:%M:%S}"
                     f"    |    Products: {len(products_data)}",
                     align='C', new_x='LMARGIN', new_y='NEXT')

            # Helper: draw a section table
            def draw_table(headers, rows, col_widths):
                """Draw a table with header + data rows."""
                # Header row
                _font('B', 7)
                pdf.set_fill_color(235, 235, 240)
                pdf.set_text_color(80, 80, 90)
                for i, h in enumerate(headers):
                    pdf.cell(col_widths[i], 7, h, border=0, fill=True)
                pdf.ln()
                # Data rows
                _font('', 7)
                pdf.set_text_color(50, 50, 55)
                alt = False
                for row in rows:
                    if alt:
                        pdf.set_fill_color(248, 248, 250)
                    else:
                        pdf.set_fill_color(255, 255, 255)
                    alt = not alt
                    # Page break check
                    if pdf.get_y() > 185:
                        pdf.add_page()
                        pdf.set_fill_color(245, 245, 247)
                        pdf.rect(0, 0, 297, 210, 'F')
                    for i, val in enumerate(row):
                        txt = _txt(val)
                        max_chars = int(col_widths[i] / 1.5)
                        if len(txt) > max_chars:
                            txt = txt[:max_chars - 2] + '..'
                        pdf.cell(col_widths[i], 6, txt, border=0, fill=True)
                    pdf.ln()
                # Bottom border line
                pdf.set_draw_color(220, 220, 225)
                pdf.set_line_width(0.2)
                pdf.line(pdf.l_margin, pdf.get_y(),
                         pdf.l_margin + sum(col_widths), pdf.get_y())

            def section_label(text):
                _font('B', 8)
                pdf.set_text_color(160, 130, 50)
                pdf.cell(0, 8, _txt(text), new_x='LMARGIN', new_y='NEXT')

            # ── Product pages ────────────────────────────────────
            prod_col_w = [14, 22, 38, 42, 42, 28, 38, 53]
            dim_col_w = [80, 80]
            veh_col_w = [32, 34, 34, 28, 28, 18, 16, 16, 24]
            oem_col_w = [80, 80]

            for idx, (prod, dims, vehs, oems) in enumerate(products_data):
                pdf.add_page()
                pdf.set_fill_color(245, 245, 247)
                pdf.rect(0, 0, 297, 210, 'F')

                # Product heading
                mann = _txt(prod[4]) if prod else f"Product {idx + 1}"
                _font('B', 13)
                pdf.set_text_color(40, 40, 45)
                pdf.cell(0, 10, f"{idx + 1}.  {mann}",
                         new_x='LMARGIN', new_y='NEXT')

                # Gold separator line
                pdf.set_draw_color(200, 168, 76)
                pdf.set_line_width(0.4)
                pdf.line(10, pdf.get_y(), 287, pdf.get_y())
                pdf.ln(5)

                # Product info
                section_label('Product Information')
                draw_table(PRODUCT_HEADERS, [prod], prod_col_w)
                pdf.ln(5)

                # Dimensions
                if dims:
                    section_label(f'Dimensions ({len(dims)})')
                    draw_table(DIM_HEADERS, dims, dim_col_w)
                    pdf.ln(5)

                # Vehicles
                if vehs:
                    section_label(f'Vehicles ({len(vehs)})')
                    draw_table(VEH_HEADERS, vehs, veh_col_w)
                    pdf.ln(5)

                # OEM Numbers
                if oems:
                    section_label(f'OEM Numbers ({len(oems)})')
                    draw_table(OEM_HEADERS, oems, oem_col_w)

            pdf.output(path)
            messagebox.showinfo("Export Complete",
                                f"PDF saved to:\n{path}", parent=win)

        # ── Export button handlers ───────────────────────────────────
        def export_selected(fmt):
            if not checked_ids:
                messagebox.showinfo("Export", "No products selected.\n\n"
                                    "Click the checkboxes to select rows.",
                                    parent=win)
                return
            product_ids = sorted(checked_ids)
            self._show_loader(loader,
                              f"Exporting {len(product_ids)} products...")

            def task():
                return [_fetch_full_product(pid) for pid in product_ids]

            def done(data):
                self._hide_loader(loader)
                if fmt == 'csv':
                    _export_csv(data)
                else:
                    _export_pdf(data)

            def worker():
                try:
                    result = task()
                    win.after(0, lambda: done(result))
                except Exception as e:
                    win.after(0, lambda: (
                        self._hide_loader(loader),
                        messagebox.showerror("Export Error", str(e),
                                             parent=win)))

            threading.Thread(target=worker, daemon=True).start()

        def export_all(fmt):
            self._show_loader(loader, "Fetching all products for export...")

            def done(data):
                self._hide_loader(loader)
                if not data:
                    messagebox.showinfo("Export", "No products to export.",
                                        parent=win)
                    return
                if fmt == 'csv':
                    _export_csv(data)
                else:
                    _export_pdf(data)

            def worker():
                try:
                    result = _fetch_all_products()
                    win.after(0, lambda: done(result))
                except Exception as e:
                    win.after(0, lambda: (
                        self._hide_loader(loader),
                        messagebox.showerror("Export Error", str(e),
                                             parent=win)))

            threading.Thread(target=worker, daemon=True).start()

        # ── Export buttons in header (top-right) ────────────────────
        _menu_font = font.Font(family="Segoe UI", size=10)
        _menu_kw = dict(
            tearoff=0, bg='#1C1C24', fg=WHITE,
            activebackground=ACCENT_GOLD, activeforeground='#0C0C0F',
            font=_menu_font, relief=tk.FLAT, bd=6,
            activeborderwidth=0, selectcolor=ACCENT_GOLD)

        # Export All dropdown
        all_menu_btn = ttk.Menubutton(hdr_export_area,
                                       text="Export All",
                                       style='Nav.TMenubutton')
        all_menu = tk.Menu(all_menu_btn, **_menu_kw)
        all_menu.add_command(
            label="     CSV   \u2014  Comma-Separated Values",
            command=lambda: export_all('csv'))
        all_menu.add_separator()
        all_menu.add_command(
            label="     PDF   \u2014  Formatted Report",
            command=lambda: export_all('pdf'))
        all_menu_btn['menu'] = all_menu
        all_menu_btn.pack(side=tk.RIGHT, padx=(6, 0), pady=6)

        # Export Selected dropdown
        sel_menu_btn = ttk.Menubutton(hdr_export_area,
                                       text="Export Selected",
                                       style='Data.TMenubutton')
        sel_menu = tk.Menu(sel_menu_btn, **_menu_kw)
        sel_menu.add_command(
            label="     CSV   \u2014  Comma-Separated Values",
            command=lambda: export_selected('csv'))
        sel_menu.add_separator()
        sel_menu.add_command(
            label="     PDF   \u2014  Formatted Report",
            command=lambda: export_selected('pdf'))
        sel_menu_btn['menu'] = sel_menu
        sel_menu_btn.pack(side=tk.RIGHT, padx=(6, 0), pady=6)

        # ── Bottom buttons (Refresh + Close only) ───────────────────
        bottom = tk.Frame(win, bg=BG_GRAY)
        bottom.pack(fill=tk.X, padx=12, pady=(8, 12))

        ttk.Button(bottom, text="Refresh", command=lambda: load_page(
            search_var.get().strip(), filter_var.get(), pag['page']),
            style='Nav.TButton').pack(side=tk.LEFT, padx=(0, 8))
        ttk.Button(bottom, text="Close", command=win.destroy,
                   style='Help.TButton').pack(side=tk.RIGHT)

        # Initial load — page 1 only
        load_page()

    def cancel_scraping(self):
        if self.is_scraping:
            self.should_stop = True
            self.log_message("Cancellation requested...", 'warning')
            self.cancel_btn.config(state='disabled')

    def show_help(self):
        messagebox.showinfo("Help", (
            "MANN-FILTER Scraper Pro\n\n"
            "1. Enter a search term range (e.g. 100 to 200)\n"
            "2. Set the page range to scrape (1–667)\n"
            "3. Click 'Start Scraping' to begin\n\n"
            "The scraper searches for matching products\n"
            "using parallel workers, extracts detailed info,\n"
            "and saves results directly to MySQL database."
        ))

    def run(self):
        self.root.mainloop()


if __name__ == "__main__":
    ScraperApp().run()

import copy
import logging
import os
import re
import smtplib
import sys
import time
import traceback
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import sqlalchemy
from dotenv import load_dotenv
from fake_useragent import UserAgent
from jinja2 import Template
from openai import OpenAI
from selenium.common.exceptions import NoSuchElementException, TimeoutException
from selenium.webdriver.chrome.service import Service as ChromeService
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait
from seleniumwire import webdriver
from sqlalchemy import create_engine
from sqlalchemy.engine.url import URL
from sqlalchemy.exc import SQLAlchemyError
from webdriver_manager.chrome import ChromeDriverManager

import config

load_dotenv()


MAX_RETRY = 5


class BusinessCrawler:
    def __init__(self):
        self.script_path = os.path.dirname(os.path.abspath(__file__))
        self.logs_folder = os.path.join(self.script_path, "logs")
        self.client = OpenAI(api_key=os.getenv("_OPENAI_API_KEY"))
        self.db_config = {
            "drivername": os.getenv("_DB_DRIVERNAME"),
            "host": os.getenv("_DB_HOST"),
            "port": int(os.getenv("_DB_PORT")),
            "username": os.getenv("_DB_USERNAME"),
            "password": os.getenv("_DB_PASSWORD"),
            "database": os.getenv("_DB_DATABASE"),
        }
        self.SQLENGHINE = create_engine(URL.create(**self.db_config))
        self.chromedriver = None
        self.logger = self.init_logger()
        self.load_business_keywords()

        self.from_email=os.getenv("_EMAIL_ACCOUNT")
        self.password=os.getenv("_EMAIL_PASSWORD")
        self.smtp_server=os.getenv("_EMAIL_SMTP")
        self.smtp_port=os.getenv("_EMAIL_PORT")

        self.logger.info("* Init BusinessCrawler")

    def init_logger(self, level=logging.INFO):
        # 오늘 날짜 폴더가 있는지 확인하고, 없으면 생성
        current_date = datetime.now().strftime("%Y%m%d")
        log_folder_path = os.path.join(self.logs_folder, current_date)
        if not os.path.exists(log_folder_path):
            os.makedirs(log_folder_path)
        log_file_path = os.path.join(log_folder_path, f"{current_date}.log")

        # 개별 로거 생성 및 설정
        logger = logging.getLogger("BusinessCrawlerLogger")
        logger.setLevel(level)

        # 로그 포맷 정의
        formatter = logging.Formatter("%(asctime)s %(levelname)s: %(message)s")
        
        # 파일 핸들러 설정
        file_handler = logging.FileHandler(log_file_path)
        file_handler.setFormatter(formatter)
        logger.addHandler(file_handler)

        # 스트림 핸들러 설정 (콘솔 출력)
        stream_handler = logging.StreamHandler()
        stream_handler.setFormatter(formatter)
        logger.addHandler(stream_handler)

        # 다른 핸들러에서 로그를 처리하게 하여, 로그 메시지가 중복으로 기록되지 않도록 설정
        logger.propagate = False
        return logger

    def init_chromedriver(self):
        options = webdriver.ChromeOptions()
        options.add_argument("--headless")
        options.add_argument("--no-sandbox")
        # fake_useragent 라이브러리를 사용하여 무작위 사용자 에이전트를 생성
        options.add_argument("user-agent=%s" % UserAgent().random)
        options.add_argument("--ignore-certificate-errors")
        # GPU 사용을 방지하여 픽셀 및 GPU 가속 비활성화
        # options.add_argument("--disable-gpu")
        # options.add_argument("--disable-software-rasterizer")
        # 자동화된 소프트웨어에서 사용되는 일부 기능들을 비활성화
        options.add_argument("--disable-blink-features=AutomationControlled")
        # Selenium이 자동화된 브라우저임을 나타내는 'enable-automation' 플래그를 비활성화합니다.
        # 브라우저가 자동화된 것처럼 보이는 몇몇 특성들을 제거
        options.add_experimental_option("excludeSwitches", ["enable-automation"])
        # 자동화 확장 기능을 비활성화합니다. 이것도 자동화 탐지를 우회하는 데 도움을 줄 수 있습니다.
        options.add_experimental_option("useAutomationExtension", False)
        # 웹 페이지에서 이미지 로딩을 차단합니다. 페이지 로딩 속도를 빠르게 하고, 데이터 사용량을 줄이는 데 유용합니다.
        # '2'는 이미지 로드를 차단하는 것을 의미합니다.
        # options.add_experimental_option(
        #     "prefs",
        #     {"profile.managed_default_content_settings.images": 2},
        # )

        # 모바일 세팅
        # user_agt = 'Mozilla/5.0 (Linux; Android 9; INE-LX1) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/96.0.4664.45 Mobile Safari/537.36'
        # options.add_argument(f'user-agent={user_agt}')
        # options.add_argument("window-size=412,950")
        # options.add_experimental_option("mobileEmulation", {
        #     "deviceMetrics": {
        #             "width": 360,
        #             "height": 760,
        #             "pixelRatio": 3.0
        #         }
        # })

        # Chrome WebDriver 생성
        self.chromedriver = webdriver.Chrome(
            service=ChromeService(ChromeDriverManager().install()),
            options=options,
        )
        # 크롤링 방지 설정을 undefined로 변경
        self.chromedriver.execute_cdp_cmd(
            "Page.addScriptToEvaluateOnNewDocument",
            {
                "source": """
                    Object.defineProperty(navigator, 'webdriver', {
                        get: () => undefined
                    })
                    """,
            },
        )

    def execute_query(self, query, data=None):
        retry_count = 0
        while True:
            try:
                # 쿼리 실행
                with self.SQLENGHINE.begin() as conn:
                    if data:
                        execute = conn.execute(sqlalchemy.text(query), data)
                    else:
                        execute = conn.execute(sqlalchemy.text(query))

                    if query.strip().lower().startswith("select") or (
                        query.strip().lower().startswith("insert")
                        and "returning" in query.lower()
                    ):
                        return execute.fetchall()
                    else:
                        # 다른 유형의 쿼리인 경우 (예: INSERT, UPDATE, DELETE)
                        return None  # 결과 없음

            except SQLAlchemyError:
                if retry_count >= MAX_RETRY:
                    raise Exception("Maximum retry atcollect_dictts reached.")
                time.sleep(retry_count)  # 다음 응답까지 retry_count초 대기
                retry_count += 1
                self.logger.info(f"Timed out, retrying ({retry_count}/{MAX_RETRY})...")

    def openai_create_nonstream(
        self,
        messages: list,
        model: str = "gpt-4-turbo-preview",
        temperature: float = 0.5,
        max_tokens: float = 2000,
    ) -> str:
        response = self.client.chat.completions.create(
            model=model,
            messages=messages,
            temperature=temperature,
            max_tokens=max_tokens,
        )
        answer = response.choices[0].message.content
        return answer

    def gpt_trimmer(self, answer):
        answer = answer.replace("\n", "<br>")
        answer = re.sub(r"\*\*(.*?)\*\*", r"<b>\1</b>", answer)
        # answer = answer.replace("""<span class="point">""", """<span style="color:#6968EC;font-weight:bold;">""")
        # answer = re.sub(r"####(.*?)<br>", r"<h4>\1<br>", answer)
        # answer = re.sub(r"###(.*?)<br>", r"<h3>\1<br>", answer)
        # answer = re.sub(r"##(.*?)<br>", r"<h2>\1<br>", answer)
        # answer = re.sub(r"##(.*?)<br>", r"<h1>\1<br>", answer)
        return answer

    def load_business_keywords(self):
        query = """SELECT keyword from mart.business_keywords"""
        result = self.execute_query(query)
        self.business_keywords = [row[0] for row in result]

    def load_to_email_list(self):
        query = """SELECT a.email
        FROM mart.address a
        JOIN mart.address_group_link agl ON a.id = agl.address_id
        WHERE agl.group_id=5;"""
        result = self.execute_query(query)
        to_email_list = [row[0] for row in result]

        query = """SELECT a.email
        FROM mart.address a
        JOIN mart.address_group_link agl ON a.id = agl.address_id
        WHERE agl.group_id=6;"""
        result = self.execute_query(query)
        cc_email_list = [row[0] for row in result]

        return to_email_list, cc_email_list

    def chk_article_exist(self, title):
        query = f"""SELECT title from mart.business_articles
        WHERE title = '{title}'"""
        result = self.execute_query(query)
        if result:
            return True
        else:
            False

    def save_articles_to_DB(self, new_articles):
        for website in new_articles:
            for article in new_articles[website]:
                insert_dict = copy.copy(article)
                insert_dict["website"]=website
                insert_dict["keywords"] = ", ".join(article["keywords"])
                insert_dict["collected_datetime"] = datetime.now()
                insert_query = """
                    INSERT INTO mart.business_articles (website, title, date, keywords, url, collected_datetime)
                    VALUES (:website, :title, :date, :keywords, :url, :collected_datetime)
                """
                self.execute_query(insert_query, insert_dict)
                
        self.logger.info("Completly Recorded Articles On The Server.")

    def make_content(self, new_articles):
        content_body = ""

        for website in new_articles:
            # content_ul = f"""<b>{website}{f" {len(new_articles[website])}개</b>" if len(new_articles[website])>0 else "</b>"}
            # <br><ul>"""
            # if len(new_articles[website])>0:
            #     for article in new_articles[website]:
            #         keyword_comment=f"""<b>[{", ".join(article["keywords"])}]</b> - """ if len(article["keywords"]) > 0 else ""                        
            #         content_ul+=f"""<li>{keyword_comment}<a href="{article["url"]}">{article["title"]}</a></li>"""
            # else:
            #     content_ul+="<li>새로 게시된 글이 없습니다.</li>"
            # content_ul += """
            # </ul><br>
            # """
            if len(new_articles[website])>0:
                content_ul = f"""
    <h6 style="margin-top:40px;margin-left:20px;margin-bottom:10px;font-family:'Malgun Gothic', 'Apple SD Gothic Neo';font-size:16px;color:#333;letter-spacing:-.05em;">{website}{f" {len(new_articles[website])}개</h6>" if len(new_articles[website])>0 else "</h6>"}
    <table width="1200" border="1" style="margin-left:20px;border-collapse:collapse;border:1px solid #d3d3d3;font-family:'Malgun Gothic', 'Apple SD Gothic Neo';font-size:13px;letter-spacing:-.05em;">
        <colgroup>
            <col width="200"/>
            <col width="*"/>
        </colgroup>
        <thead>
            <tr style="height:40px;background-color:#f4f4f4;color:#222;">
                <th style="border-color:#e4e5e7;border-top:2px solid #333;">키워드</th>
                <th style="border-color:#e4e5e7;border-top:2px solid #333;">모집공고 바로가기</th>
                <th style="border-color:#e4e5e7;border-top:2px solid #333;">등록일</th>
            </tr>
        </thead>
        <tbody style="text-indent:5px;">"""
                for article in new_articles[website]:
                    keyword_comment=f"""<b>{", ".join(article["keywords"])}</b>""" if len(article["keywords"]) > 0 else "&nbsp;"                        
                    content_ul+=f"""<tr style="height:40px;">
            <th style="border-color:#e4e5e7">{keyword_comment}</th>
            <td style="border-color:#e4e5e7">♾️ <a href="{article["url"]}" style="color:#333">{article["title"]}</a></td>
            <th style="border-color:#e4e5e7">{article["date"]}</th>
        </tr>"""
                content_ul+="""
        </tbody>
    </table>"""
            else:
                content_ul=f"""<h6 style="margin-top:40px;margin-left:20px;margin-bottom:10px;font-family:'Malgun Gothic', 'Apple SD Gothic Neo';font-size:16px;color:#333;letter-spacing:-.05em;">{website} - <span style="color:#999;font-size:14px;">❌ 새로 게시된 글이 없습니다.</span></h6>"""
            content_body+=content_ul

        return content_body

    def send_email(self, to_email_list, cc_email_list, subject, content):
        msg = MIMEMultipart()
        msg["From"] = self.from_email
        msg["To"] = ", ".join(to_email_list)
        msg["Cc"] = ", ".join(cc_email_list) if cc_email_list else None
        msg["Subject"] = subject
        msg.attach(MIMEText(content, "html"))

        with smtplib.SMTP(self.smtp_server, self.smtp_port) as server:
            server.starttls()
            server.login(self.from_email, self.password)
            server.sendmail(self.from_email, to_email_list + cc_email_list if cc_email_list else to_email_list, msg.as_string())
        self.logger.info("Email Send Complete.")

    def error_report(self, error_msg, error_traceback): 
        current_datetime = datetime.now().strftime("%Y%m%d%H%M%S")
        subject = f"""[사업공고 크롤링] {error_msg}"""  

        current_date = datetime.now().strftime("%Y%m%d")
        log_folder_path = os.path.join(self.logs_folder, current_date)
        with open(
            os.path.join(log_folder_path, "err_html.html"), "w", encoding="utf-8"
        ) as output_file:
            with open(os.path.join(self.script_path, "src/j2_err_template.html")) as template_file:
                j2_template = Template(template_file.read())
                email_html_src = j2_template.render(
                    current_datetime=current_datetime,
                    error_msg=error_msg,
                    error_traceback=error_traceback,
                )
                output_file.write(email_html_src)

        to_email_list=["sjwang@ksystem.co.kr", "jslee2108@ksystem.co.kr"]
        # cc_email_list=["biteam@ksystem.co.kr"]
        # to_email_list=["sjwang@ksystem.co.kr"]
        cc_email_list=[]

        # 이메일 발송
        self.send_email(to_email_list, cc_email_list, subject, email_html_src)

    def refresh(self, website_src, page):
        self.chromedriver.get(website_src["url"])
        time.sleep(2)
        # 첫 페이지 수집 작업 완료 시 페이지 이동
        if page != website_src["init_page_idx"]:
            btn_pages = self.chromedriver.find_elements(
                By.CSS_SELECTOR, website_src["css_pages"]
            )
            self.chromedriver.execute_script("arguments[0].click();", btn_pages[page])
            time.sleep(2)

        articles = WebDriverWait(self.chromedriver, 10).until(
            EC.presence_of_all_elements_located(
                (
                    By.CSS_SELECTOR,
                    website_src["css_articles"],
                ),
            ),
        )
        return articles

    def crawler(self, website_title):
        try:
            start_time = time.time()

            new_articles = []
            collect_titles = []
            continue_flag = True
            website_src = config.WEBSITES[website_title]

            # self.logger.info(f"""{website_title} 수집 시작""")
            # self.logger.info(website_src["url"])
            self.chromedriver.get(website_src["url"])
            time.sleep(3)
            page = website_src["init_page_idx"]

            # 전체 수집 개수 설정
            while continue_flag:
            # while continue_flag and len(collect_list)<30:
                # 첫 페이지 수집 작업 완료 시 페이지 이동
                if page != website_src["init_page_idx"]:
                    btn_pages = self.chromedriver.find_elements(
                        By.CSS_SELECTOR, website_src["css_pages"]
                    )
                    # 이동할 페이지가 없다면 종료
                    if len(btn_pages) <= page:
                        break
                    self.chromedriver.execute_script(
                        "arguments[0].click();", btn_pages[page]
                    )
                    # self.logger.info("다음 페이지 이동")
                    time.sleep(3)

                article_idx = website_src["init_article_idx"]
                while True:
                    try:
                        articles = WebDriverWait(self.chromedriver, 10).until(
                            EC.presence_of_all_elements_located(
                                (
                                    By.CSS_SELECTOR,
                                    website_src["css_articles"],
                                ),
                            ),
                        )
                    except TimeoutException:
                        articles = self.refresh(website_src, page)

                    # 사이트별 수집 개수 설정
                    # 페이지 내 게시글 수집 완료 시 break
                    if article_idx == len(articles):break

                    try:
                        btn_title = articles[article_idx].find_element(
                            By.CSS_SELECTOR, website_src["css_title"]
                        )
                        title = btn_title.text.strip().replace("\n", "").replace("'", '"')
                    except NoSuchElementException:
                        # self.logger.info("지나감")
                        break

                    # 서버 수집 여부 확인
                    if self.chk_article_exist(title):
                        # self.logger.info(f"""{title} 게시물은 기 수집된 내용으로 {website_title}의 수집을 종료합니다.""")
                        continue_flag = False
                        break

                    # 기존 수집 여부 확인
                    if title in collect_titles:
                        article_idx += 1
                        continue

                    collect_dict = {}
                    # 게시글 제목 수집
                    collect_dict["title"] = title
                    collect_titles.append(title)
                    # 게시글 날짜 수집
                    date = (
                        articles[article_idx]
                        .find_element(By.CSS_SELECTOR, website_src["css_date"])
                        .text.strip()
                    )
                    collect_dict["date"] = date

                    # 포함 키워드 수집
                    collect_dict["keywords"] = []
                    for keyword in self.business_keywords:
                        if keyword in title:
                            collect_dict["keywords"].append(keyword)

                    if "inner_href" in website_src:
                        # 게시글 url 수집
                        if website_src["inner_href"]["css_href"]:
                            btn_href = articles[article_idx].find_element(
                                By.CSS_SELECTOR, website_src["inner_href"]["css_href"]
                            )
                        else:
                            btn_href = articles[article_idx]
                        collect_dict["url"] = eval(website_src["inner_href"]["url_src"])
                    else:
                        # 새 탭으로 열리는 옵션 제거
                        self.chromedriver.execute_script(
                            "arguments[0].setAttribute('target', '_self');", btn_title
                        )
                        # 게시글 제목 클릭
                        self.chromedriver.execute_script(
                            "arguments[0].click();", btn_title
                        )
                        time.sleep(1)
                        # 게시글 url 수집
                        collect_dict["url"] = self.chromedriver.current_url
                        self.chromedriver.back()
                        time.sleep(2)

                    # self.logger.info(collect_dict)

                    new_articles.append(collect_dict)
                    article_idx += 1
                page += 1

            runtime = round(time.time() - start_time)
            runtime = (
                f"""{f"{runtime//60}분 " if runtime//60>0  else ""}{runtime%60}초."""
            )
            self.logger.info(
                f"""{website_title} : {len(new_articles)} Articles Collected. runtime : {runtime}"""
            )

            return new_articles
        except Exception:
            error_msg = f"""{website_title} 수집 중 알 수 없는 에러가 발생하였습니다."""
            error_traceback = f"{traceback.format_exc()}"
            self.logger.error(error_msg)
            self.logger.error(error_traceback)
            self.error_report(error_msg, error_traceback)

    def email_worker(self, new_articles):
        # 이메일 제목
        new_articles_num=0
        for website in new_articles:
            new_articles_num+=len(new_articles[website])
        subject = f"[{datetime.now().strftime("%Y-%m-%d")}] 새로 게시된 {new_articles_num}개 사업 공고 게시글이 있어요!"
        
        # 수집 키워드 정리
        collected_keywords=[]
        for website in new_articles:
            for article in new_articles[website]:
                collected_keywords.extend(article["keywords"])
        collected_keywords = list(set(collected_keywords))
        self.logger.info(f"""Today's Collected Keywords : {collected_keywords}""")

        # 수집된 키워드가 있을 경우 프롬프트 추가
        keywords_prompt = ""
        if len(collected_keywords) > 0:
            keywords_prompt = f"""
그리고 {", ".join(collected_keywords)} 키워드를 포함한 공고들이 게시되었으니 관심있게 볼 것을 제안해
- 키워드 들은 <span style="color:#6968EC;font-weight:bold;"></span> 태그로 감싸서 대답해"""
            
        # greet 생성
        greet_prompt = config._GREET_PROMPT.format(
            new_articles_num=new_articles_num,
            keywords_prompt=keywords_prompt
        )

        messages=[{"role":"system", "name":"KBot", "content":greet_prompt}]
        content_greet = self.openai_create_nonstream(messages)
        content_greet = self.gpt_trimmer(content_greet)
        self.logger.info(f"""Generated Greet : {content_greet}""")

        # articles
        content_body = self.make_content(new_articles)

        current_date = datetime.now().strftime("%Y%m%d")
        log_folder_path = os.path.join(self.logs_folder, current_date)
        with open(
            os.path.join(log_folder_path, "email_html.html"), "w", encoding="utf-8"
        ) as output_file:
            with open(os.path.join(self.script_path, "src/j2_template_inline.html")) as template_file:
                j2_template = Template(template_file.read())
                email_html_src = j2_template.render(
                    content_greet=content_greet,
                    content_body=content_body,
                )
                output_file.write(email_html_src)

        # 보내는 이메일 리스트 로드
        to_email_list, cc_email_list=self.load_to_email_list()

        # 이메일 발송
        self.send_email(to_email_list, cc_email_list, subject, email_html_src)


    def test(self, GPT_flag):
        self.logger.info("Operating Test Process")
        today = datetime.now().strftime("%Y-%m-%d")

        insert_query = f"""
        SELECT website, title, date, keywords, url FROM mart.business_articles
        WHERE collected_datetime >= '{today}'
        ORDER BY collected_datetime
        """

        result = business_crawler.execute_query(insert_query)

        new_articles={}
        for key in list(config.WEBSITES.keys()):
            new_articles[key]=[]

        for row in result:                
            temp_list={}
            temp_list["title"]=row[1]
            temp_list["date"]=row[2]
            temp_list["keywords"]=row[3].split(", ") if row[3] != "" else []
            temp_list["url"]=row[4]
            new_articles[row[0]].append(temp_list)
            
        # 이메일 제목
        new_articles_num=0
        for website in new_articles:
            new_articles_num+=len(new_articles[website])
        subject = f"[{datetime.now().strftime("%Y-%m-%d")}] 새로 게시된 {new_articles_num}개 사업 공고 게시글이 있어요!"
        
        # 수집 키워드 정리
        collected_keywords=[]
        for website in new_articles:
            for article in new_articles[website]:
                collected_keywords.extend(article["keywords"])
        collected_keywords = list(set(collected_keywords))
        self.logger.info(f"""Today's Collected Keywords : {collected_keywords}""")
        
        if(GPT_flag):
            # 수집된 키워드가 있을 경우 프롬프트 추가
            keywords_prompt = ""
            if len(collected_keywords) > 0:
                keywords_prompt = f"""
    그리고 {", ".join(collected_keywords)} 키워드를 포함한 공고들이 게시되었으니 관심있게 볼 것을 제안해
    - 키워드 들은 <span style="color:#6968EC;font-weight:bold;"></span> 태그로 감싸서 대답해"""

            # greet prompt 생성
            greet_prompt = config._GREET_PROMPT.format(
                new_articles_num=new_articles_num,
                keywords_prompt=keywords_prompt
            )
            
            messages=[{"role":"system", "name":"KBot", "content":greet_prompt}]
            content_greet = self.openai_create_nonstream(messages)
            content_greet = self.gpt_trimmer(content_greet)
            self.logger.info(f"""Generated Greet : {content_greet}""")
        else:
            content_greet=config.GREET_SAMPLE

        # articles
        content_body = self.make_content(new_articles)

        current_date = datetime.now().strftime("%Y%m%d")
        log_folder_path = os.path.join(self.logs_folder, current_date)
        with open(
            os.path.join(log_folder_path, "email_html.html"), "w", encoding="utf-8"
        ) as output_file:
            with open(os.path.join(self.script_path, "src/j2_template_inline.html")) as template_file:
                j2_template = Template(template_file.read())
                email_html_src = j2_template.render(
                    content_greet=content_greet,
                    content_body=content_body,
                )
                output_file.write(email_html_src)

        # 보내는 이메일 리스트 로드
        # to_email_list, cc_email_list=self.load_to_email_list()
        # sjwang@ksystem.co.kr
        # jslee2108@ksystem.co.kr
        # jhkim1910@ksystem.co.kr
        to_email_list=["sjwang@ksystem.co.kr"]
        cc_email_list=[]

        # 이메일 발송
        self.send_email(to_email_list, cc_email_list, subject, email_html_src)
        
    def run(self):
        try:
            self.logger.info("Operating Business Crawling.")
            start_time = time.time()

            # chromedriver 생성
            self.init_chromedriver()

            # 게시글 수집
            new_articles = {}
            for key in list(config.WEBSITES.keys()):
                new_articles[key] = self.crawler(key)

            # DB 저장
            self.save_articles_to_DB(new_articles)

            # 이메일 작업
            self.email_worker(new_articles)

            self.logger.info("* Complete Business Crawling Process.")
            currenttime = f"""{datetime.now().strftime("%Y년 %m월 %d일 %p %I시 %M분")}"""
            self.logger.info(currenttime)
            runtime = round(time.time() - start_time)
            runtime = f"""Runtime : {f"{runtime//60} min " if runtime//60>0  else ""}{runtime%60} sec."""
            self.logger.info(runtime)
            self.logger.info(f"""Total {len(new_articles)} Articles Collected.""")

        except Exception:
            error_msg = """서비스에 운영 중 문제가 발생하였습니다."""
            error_traceback = f"{traceback.format_exc()}"
            self.logger.error(error_msg)
            self.logger.error(error_traceback)
            self.error_report(error_msg, error_traceback)
        finally:
            if self.chromedriver:
                self.chromedriver.quit()
                self.logger.info("Chromdriver Quit")


if __name__ == "__main__":
    execute = sys.argv[1]

    business_crawler = BusinessCrawler()

    if execute == "Service" : 
        business_crawler.run()
    elif execute == "TEST" : 
        GPT_flag = int(sys.argv[2])
        business_crawler.test(GPT_flag)
    else:
        business_crawler.logger.warning("Executor Is Not Proper")

# Not stdlib
import aiohttp  # ClientSession
import bs4  # BeautifulSoup
# Stdlib
import asyncio  # run, gather
from datetime import datetime
import os, sys, subprocess # listdir, startfile
from time import perf_counter
import itertools as it  # chain, count
import json  # load
from getpass import getpass
from http import HTTPStatus
from argparse import ArgumentParser
# Awesome decorator inspired by Veky (from py.checkio.org).
from functools import partial
aggregate = partial(partial, lambda f, g: lambda *a, **kw: f(g(*a, **kw)))


class WebError(Exception):
    """ Class for considered web errors. """


# ---------------------- Constants & Global variables ---------------------- #
GITHUB = 'https://github.com'
GITHUB_API = 'https://api.github.com'
OUTPUT = 'github-pulls.html'
API_ERRORS = {
    401: 'Wrong authentication?',
    403: '''You probably reached the rate limit of the github API:
  * 60 requests per hour (IP related) if you're not authenticated.
  * 5000 requests per hour (account related) if you are.
This tool does a request for every hundred repositories that users have so:
  * JSON file maybe have too much users or you use this tool too much.
  * you can authenticate or wait some time.''',
    404: 'Maybe one user does not exists.',
    }

now = datetime.now().replace(microsecond=0)
nb_web_requests = 0
repos_with_issues = []

# ----------------------------- ArgumentParser ----------------------------- #
parser = ArgumentParser(
    description='Parse github repositories for opened pull requests & issues.',
    epilog="""Give github usernames or a json file {user: [repository, ...]}.
Authenticate if you had an error message for (repeated?) big requests.""")
# Simple use: by github usernames.
parser.add_argument('-u', '--user', nargs='+',
                    help="Look users' repositories.")
# Customizable use with a json file.
parser.add_argument('-j', '--json',
                    help='JSON file with repositories '
                    '(default: first json file found in current folder).')
# Simple filter of results: useful to get only latest results, if there has.
parser.add_argument('-d', '--days', type=int,
                    help='only ones opened in the last ... days'
                    ' (default: all).')
# Sort the results for a better visualization.
parser.add_argument('-s', '--sort',
                    choices=['opening', 'repo', 'author'], default='opening',
                    help='sorting output (default: by %(default)s)')
parser.add_argument('--auth', action='store_true',
                    help='Authenticate to the github API with prompts.')
args = parser.parse_args()

if args.days is not None and args.days <= 0:
    parser.error('days argument must be positive.')


def get_repos() -> list:
    """ Define repos to watch according to given arguments user/json. """
    def fix_name(name: str) -> str: return name.replace(' ', '-')
    if args.user:
        users = list(map(fix_name, args.user))
        data = get_repos_to_watch_from(users)
    else:
        try:
            file = args.json or next(f for f in os.listdir('.')
                                     if f.endswith('.json'))
            with open(file) as f:
                data = json.load(f)
        except (StopIteration, FileNotFoundError):
            parser.error('No json file found.')
        except json.JSONDecodeError:
            parser.error('Failed to decode the json file.')
        if not (isinstance(data, dict) and
                all(isinstance(user, str) and isinstance(repos, list) and
                    all(isinstance(repo, str) for repo in repos)
                    for user, repos in data.items())):
            parser.error("The given file does not have the appropriate "
                         "structure: {user1: [repo1, ...], ...}.")
        users = list(map(fix_name, data))
        data = {(fix_name(user), fix_name(repo))
                for user, repos in data.items() for repo in repos}
        # Thanks to github api.
        # We will only parse wanted repositories with issues/pulls.
        data &= get_repos_to_watch_from(users)
    return list(data)


def recent_enough(timedelta) -> bool:
    return args.days is None or timedelta.total_seconds() < 60*60*24*args.days


def sorting_key(x) -> tuple:
    """ Sort pulls and issues according to sort argument. """
    # x = (since, user, repo, title, link, opened_by)
    if args.sort == 'opening':
        return x
    if args.sort == 'repo':
        return x[1].casefold(), x[2].casefold(), x[0]
    if args.sort == 'author':
        return x[5].casefold(), x[0]


def authentication():
    if args.auth:
        global timing
        # Decrease `timing` by the time to authenticate.
        timing += perf_counter()

        username = input('Your GitHub username: ')
        prompt = f'Enter host password for user {username!r}:'
        auth = aiohttp.BasicAuth(login=username, password=getpass(prompt))

        timing -= perf_counter()
        return auth


# ----------------------- Analyze github source code ----------------------- #
def github_div_search(tag: bs4.Tag) -> bool:
    """ Is it a div tag for a pull request or an issue ? """
    return (tag.name == 'div' and tag.has_attr('class') and
            {'float-left', 'lh-condensed', 'p-2'} <= set(tag.attrs['class']))


def github_number_of_issues(soup: bs4.BeautifulSoup,
                            user: str, repo: str) -> int:
    """ Find the number of issues in soup of '/user/repo/pulls'. """
    # <a ... href="/USER/REPOSITORY_NAME/issues" ...>
    #     <svg class="octicon octicon-issue-opened" ...>...</svg>
    #     <span itemprop="name">Issues</span>
    #     <span class="Counter">NUMBER WE WANT</span>
    #     <meta itemprop="position" content="2">
    # </a>
    link = soup.find('a', {'href': f'/{user}/{repo}/issues'})
    try:
        span = link.find('span', {'class': 'Counter'})
        return int(span.text.replace(',', ''))  # '1,061' -> 1061
    except AttributeError:  # link or span can be None.
        return 0


def github_parser(html_text: str, user: str, repo: str, look_issues: bool):
    """ Parse github source code to detect pulls/issues
        and generate useful contents, when there are recent enough.
        Add the repo to repos_with_issues when
        look_issues is True and there are issues.
        Finally generate the url to the next page when we must continue. """
    soup = bs4.BeautifulSoup(html_text, 'html.parser')
    if look_issues and github_number_of_issues(soup, user, repo):
        repos_with_issues.append((user, repo))
    for div in soup.find_all(github_div_search):
        link, opened_by = div.a, div.find('span', {'class': 'opened-by'})
        opening_time = opened_by.find('relative-time')['datetime']
        since = now - datetime.strptime(opening_time, '%Y-%m-%dT%H:%M:%SZ')
        # Sorted by newest so no need to continue when one is too old.
        if not recent_enough(since):
            # We should stop the search. Don't give a link to the next page.
            yield
            return
        labels = div.find_all('a', {'class': 'IssueLabel'})
        milestones = div.find_all('a', {'class': 'milestone-link'})
        yield (since, user, repo, link.text, link['href'], opened_by.a.text,
               [(tag.text, tag['href']) for tag in labels],
               [(tag.text, tag['href']) for tag in milestones])
    # The search should stop if there is no link to the next page.
    link = soup.find('a', {'class': 'next_page', 'rel': 'next'}, text='Next')
    yield GITHUB + link['href'] if link else None


# ---------- Asynchronous way to get github api/pulls/issues pages ---------- #
async def get_html_json(session: aiohttp.ClientSession, url: str) -> str:
    global nb_web_requests
    nb_web_requests += 1
    async with session.get(url) as response:
        status = response.status
        if status != 200:
            raise WebError(url, status, API_ERRORS.get(status, ''))
        return await response.json()


async def get_html_text(session: aiohttp.ClientSession, url: str) -> str:
    global nb_web_requests
    nb_web_requests += 1
    async with session.get(url) as response:
        return await response.text()


@aggregate(asyncio.run)
async def get_repos_to_watch_from(users: list) -> set:
    """ All repositories of each user, thanks to github api. """
    repos = set()

    async def task(user: str):
        for page in it.count(1):
            url = f'{GITHUB_API}/users/{user}/repos?per_page=100&page={page}'
            new_data = await get_html_json(session, url)
            for repo in new_data:
                if repo['open_issues']:  # or open pull requests
                    new = repo['full_name'].split('/')
                    repos.add(tuple(new))
            if len(new_data) < 100:
                break

    async with aiohttp.ClientSession(auth=authentication()) as session:
        await asyncio.gather(*map(task, users))
        return repos


@aggregate(asyncio.run)
async def opened(repos: list, what: str, look_issues: bool = False) -> list:
    """ Look "what" in the given repositories, in an efficient way.
        Return sorted list of generated contents. """
    async def parser_what_from(github_repo) -> list:
        user, repo = github_repo
        url = f'{GITHUB}/{user}/{repo}/{what}'
        res = []
        while True:
            text = await get_html_text(session, url)
            *L, url = github_parser(text, user, repo, look_issues)
            res.extend(L)
            if not url:
                return res

    async with aiohttp.ClientSession(raise_for_status=True) as session:
        tasks = map(parser_what_from, repos)
        results = await asyncio.gather(*tasks)
        return sorted(it.chain.from_iterable(results), key=sorting_key)


# ----------------------------- HTML/CSS output ----------------------------- #
CSS = '''
body { background-color: #CAEBFB; }
table { border-collapse: collapse; }
caption { color: #163E69; font-size: 24px; font-weight: bold; }
th, td { padding: 8px; text-align: left; }
th { color: #0C64B4; }
tr:nth-child(even), th { background-color: #eee; }
tr:nth-child(odd) { background-color: #ddd; }
tr:hover { background-color: #0C64B4; color: #CAEBFB; }
a { text-decoration: none; color: inherit; }
'''


@aggregate(''.join)
def html_table(list_opened: list, what: str):
    """ Previously generated content presented in an html table. """
    if list_opened:  # Otherwise, it would be an empty table, so yield nothing.
        any_label, any_milestone = (any(x[i] for x in list_opened)
                                    for i in (-2, -1))
        th_labels = '<th>Labels</th>' if any_label else ''
        th_milestones = '<th>Milestones</th>' if any_milestone else ''
        yield f'''
<table>
    <caption>Opened {what.lower()}s</caption>
    <thead>
        <th>Username</th>
        <th>Repository</th>
        <th>{what.capitalize()}</th>
        {th_labels}
        {th_milestones}
        <th>Opened by</th>
        <th>Since</th>
    </thead>'''
        for (since, user, repo, title, link, opened_by,
             labels, milestones) in list_opened:
            labels, milestones = ('<br>'.join(f'<a href="{GITHUB}{a}">{t}</a>'
                                              for t, a in L)
                                  for L in (labels, milestones))
            td_labels = f'<td>{labels}</td>' if any_label else ''
            td_milestones = f'<td>{milestones}</td>' if any_milestone else ''
            yield f'''
    <tr>
        <td><a href="{GITHUB}/{user}">{user}</a></td>
        <td><a href="{GITHUB}/{user}/{repo}">{repo}</a></td>
        <td><a href="{GITHUB}{link}">{title}</a></td>
        {td_labels}
        {td_milestones}
        <td><a href="{GITHUB}/{opened_by}">{opened_by}</a></td>
        <td>{since}</td>
    </tr>'''
        yield '''
</table>'''


def html_template(pulls: list, issues: list) -> str:
    """ Create full html code source. """
    return f'''<!DOCTYPE html>
<html>
    <head>
        <title>Opened pull requests and issues (sorted)</title>
        <style>{CSS}</style>
    </head>
    <body>
        <p>
            Took {timing:.1f} seconds to do {nb_web_requests} web requests,
            obtain {len(pulls)} opened pull request(s)
            and {len(issues)} opened issue(s).
        </p>
        {html_table(pulls, 'pull request')}
        <br>
        {html_table(issues, 'issue')}
    </body>
</html>
'''


def open_file(filename):
    if sys.platform == "win32":
        os.startfile(filename)
    else:
        opener ="open" if sys.platform == "darwin" else "xdg-open"
        subprocess.call([opener, filename])


# -------------------------------- Main part -------------------------------- #
def main():
    """ Looking for opened pulls and issues.
        Write and open a great html file with them,
        only when there is something to show. """
    global timing
    timing = - perf_counter()

    try:
        # Look pulls pages of all repositories: looking for pull requests,
        # and the number of issues (update `repos_with_issues`).
        pulls = opened(get_repos(), 'pulls', look_issues=True)
        # Then look issues pages when there are issues.
        issues = opened(repos_with_issues, 'issues')
    except WebError as error:
        url, status, message = error.args
        error = next((k.replace('_', ' ')
                      for k, v in HTTPStatus.__members__.items()
                      if v == status), '')
        print(f'ERROR {status} {error}: {url}\n{message}')
        exit(1)

    timing += perf_counter()

    if pulls or issues:
        text = html_template(pulls, issues)
        with open(OUTPUT, 'w', encoding='utf-8') as file:
            file.write(text)
        open_file(OUTPUT)


if __name__ == '__main__':
    main()

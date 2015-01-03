from datetime import datetime
import hashlib
import os
import os.path
from flask import url_for, flash
from flask.ext.login import current_user
from sqlalchemy import or_, and_
from sqlalchemy.exc import IntegrityError
from literable import db, app, book_staging_upload_set, tmp_cover_upload_set, epub
from literable.orm import Book, User, ReadingList, Taxonomy, Rating, ReadingListBookAssociation


def _get_page(page):
    if page is None:
        page = 1
    else:
        page = int(page)
    return page


def _privilege_filter():
    return or_(current_user.admin, Book.user_id == current_user.id, Book.public)


def user_can_modify_book(book, user):
    return user.admin or book.user_id == current_user.id


def user_can_download_book(book, user):
    return book.public or user_can_modify_book(book, user)


def get_books(page):
    page = max(1, _get_page(page))
    return Book.query.order_by(Book.title).paginate(page, per_page=app.config['BOOKS_PER_PAGE'])


def get_all_books():
    return Book.query.order_by(Book.title).all()


def get_book(id):
    if id is None:
        return Book()  # blank book object
    else:
        return Book.query.get_or_404(id)


def get_recent_books(page):
    page = max(1, _get_page(page))
    return Book.query.filter(_privilege_filter()).order_by('created_at desc, id desc').paginate(page, per_page=app.config['BOOKS_PER_PAGE'])


def search_books(q):
        return Book.query.filter(and_(Book.title.ilike("%"+q+"%"), _privilege_filter())).order_by('created_at desc, id desc')


def get_incomplete_books():
    books = {
        'without a cover': [],
        'without a description': [],
        'without a file': [],
        'without an author': [],
        'without a genre': [],
        'without a publisher': [],
        'that are duplicate': []
    }

    book_titles = set()

    for book in get_all_books():
        if book.title in book_titles:
            books['that are duplicate'].append(book)
        else:
            book_titles.add(book.title)

        if not book.authors:
            books['without an author'].append(book)
        if not book.cover:
            books['without a cover'].append(book)
        if not book.filename:
            books['without a file'].append(book)
        if not book.description:
            books['without a description'].append(book)
        if not book.genres:
            books['without a genre'].append(book)
        if not book.publishers:
            books['without a publisher'].append(book)

    return books

def get_taxonomy_books(tax_type, tax_slug, page=None):
    page = max(1, _get_page(page))


    tax = Taxonomy.query.filter_by(type=tax_type, slug=tax_slug).first_or_404()
    q = Book.query.filter(and_(Book.taxonomies.any(Taxonomy.id == tax.id), _privilege_filter()))

    if tax_type == 'series':
        q = q.order_by(Book.series_seq, Book.title)
    else:
        q = q.order_by(Book.title)

    books = q.paginate(page, per_page=app.config['BOOKS_PER_PAGE'])
    books = None if len(books.items) == 0 else books

    return (books, tax)


def get_taxonomy_terms(ttype):
    return Taxonomy.query.filter_by(type=ttype).order_by(Taxonomy.name)


def get_taxonomy_terms_and_counts(ttype, order=None):
    return Taxonomy.get_grouped_counts(ttype, order)


def get_taxonomy_types():
    return Taxonomy.get_types()


def get_taxonomies_and_terms():
    taxonomies = {}
    for ttype, hierarchical in get_taxonomy_types().iteritems():
        terms = get_taxonomy_terms_and_counts(ttype)

        taxonomies[ttype] = {
            'hierarchical': hierarchical,
            'terms': terms
        }
    # for tax in Taxonomy.query.order_by(Taxonomy.name).all():
    #     if tax.type not in taxonomies:
    #         taxonomies[tax.type] = {'hierarchical': False, 'terms': []}
    #
    #     taxonomies[tax.type]['terms'].append(tax)
    #
    #     if tax.parent_id and not taxonomies[tax.type]['hierarchical']:
    #         taxonomies[tax.type]['hierarchical'] = True

    return taxonomies


def add_book(form, files):
    if 'title' not in form or not form['title'].strip():
        raise ValueError("Title must not be blank")

    book = Book()
    book.title = form['title']
    book.description = form['description']
    book.series_seq = int(form['series_seq']) if form['series_seq'] else None
    book.public = True if form['privacy'] == 'public' else False
    book.user = current_user
    book.created_at = datetime.now()

    book.update_taxonomies({
        'author':  [(form['author'], form['author_sort'])],
        'publisher': [form['publisher']],
        'series': [form['series']],
        'genre': [form['genre']],
        'tag': form['tags'].split(','),
    })

    if 'meta-cover' in form and form['meta-cover']:
        book.move_cover_from_tmp(form['meta-cover'])
    elif 'cover' in files:
        book.attempt_to_update_cover(files['cover'])

    if 'file' in form and form['file']:
        book.move_file_from_staging(form['file'])

    db.session.add(book)
    db.session.commit()

    if app.config['WRITE_META_ON_SAVE']:
        book.write_meta()

    return True


def edit_book(id, form, files):
    book = get_book(id)
    if book:
        if not user_can_modify_book(book, current_user):
            return False

        book.title = form['title']
        book.description = form['description']
        book.series_seq = int(form['series_seq']) if form['series_seq'] else None
        book.public = True if form['privacy'] == 'public' else False

        book.update_taxonomies({
            'author': [(form['author'], form['author_sort'])],
            'publisher': [form['publisher']],
            'series': [form['series']],
            'genre': [form['genre']],
            'tag': form['tags'].split(','),
        })

        if 'meta-cover' in form and form['meta-cover']:
            book.move_cover_from_tmp(form['meta-cover'])
        elif 'cover' in files and files['cover']:
            book.attempt_to_update_cover(files['cover'])

        if 'file' in form and form['file']:
            book.move_file_from_staging(form['file'])

        db.session.commit()

        if app.config['WRITE_META_ON_SAVE']:
            book.write_meta()

        return True
    return False


def upload_book(file):
    filename = book_staging_upload_set.save(file)
    if filename:
        extension = os.path.splitext(filename)[1][1:]
        if extension == 'epub':
            e = epub.Epub(book_staging_upload_set.path(filename))
            if e:
                meta = e.metadata
                if 'creator' in meta:
                    meta['author'] = meta['creator']
                    del meta['creator']

                # if the book has a cover, copy it to tmp directory
                if e.cover:
                    cover_filename = os.path.basename(e.cover)
                    # todo conflicts
                    if os.path.exists(tmp_cover_upload_set.path(cover_filename)):
                        cover_filename = tmp_cover_upload_set.resolve_conflict(tmp_cover_upload_set.config.destination, cover_filename)

                    dest = tmp_cover_upload_set.path(cover_filename)
                    extracted = e.extract_cover(dest)

                    if extracted:
                        meta['cover'] = cover_filename
            else:
                meta = None
        else:
            meta = None

        return filename, meta
    else:
        return None


def rate_book(book_id, score):
    rating = Rating.query.filter_by(book_id=book_id, user_id=current_user.id).first()
    if rating:
        rating.rating = score
    else:
        rating = Rating()
        rating.user_id = current_user.id
        rating.book_id = book_id
        rating.rating = score
        db.session.add(rating)

    db.session.commit()


def delete_book(id):
    book = get_book(id)

    if not user_can_modify_book(book, current_user):
        flash('You cannot delete a book you do not own', 'error')
        return False

    try:
        book.remove_file()
    except:
        flash('Unable to delete file', 'error')

    try:
        book.remove_cover()
    except:
        flash('Unable to delete cover', 'error')

    db.session.delete(book)
    db.session.commit()


def get_taxonomy_terms_without_parent(ttype):
    return Taxonomy.query.filter_by(parent_id=None, type=ttype).order_by(Taxonomy.name).all()


def add_taxonomy(name, ttype, parent=None):
    tax = Taxonomy()
    tax.name = name
    tax.slug = tax.generate_slug()
    tax.type = ttype
    tax.parent_id = parent if parent else None
    db.session.add(tax)
    db.session.commit()
    return tax.id


def edit_taxonomy(data):
    new = False
    if 'id' in data and data['id']:
        tax = Taxonomy.query.get(data['id'])
    else:
        new = True
        tax = Taxonomy()
        tax.type = data['type']

    if not tax:
        return False

    tax.name = data['name']
    tax.name_sort = data['name']
    tax.slug = tax.generate_slug()

    if 'parent' in data and data['parent']:
        tax.parent_id = int(data['parent'])
    else:
        tax.parent_id = None

    if new:
        db.session.add(tax)

    db.session.commit()

    return True


def delete_taxonomy(data):
    if 'id' not in data:
        return False

    tax = Taxonomy.query.get(data['id'])
    if not tax:
        return False

    db.session.delete(tax)
    db.session.commit()
    return True


def delete_tax_if_possible(tax, id):
    pass
    # obj = {
    #     'genre': Genre,
    #     'tag': Tag,
    #     'series': Series,
    #     'author': Author,
    #     'publisher': Publisher,
    # }[tax]
    #
    # instance = obj.query.get(int(id))
    # if instance:
    #     if len(instance.books) == 0:  # no books left, so we can delete
    #         delete_tax(tax, [id])


def add_user(username, password):
    u = User()
    u.username = username
    u.set_password(password)
    u.admin = False
    db.session.add(u)
    db.session.commit()
    return u.id


def get_users():
    return User.query.order_by(User.username).all()


def delete_user(username):
    u = User.query.filter_by(username=username).first()
    db.session.delete(u)
    db.session.commit()


def delete_reading_list(list_id):
    rlist = ReadingList.query.filter_by(id=list_id).first()
    if rlist:
        if rlist.user_id == current_user.id:
            db.session.delete(rlist)
            db.session.commit()
        else:
            raise
    else:
        raise


def new_reading_list(name):
    rlist = ReadingList()
    rlist.user_id = current_user.id
    rlist.name = name
    rlist.slug = rlist.generate_slug()

    db.session.add(rlist)
    db.session.commit()


def update_reading_list_order(list_id, ordering):
    rlist = ReadingList.query.filter_by(id=list_id).first()
    for rbook in rlist._books:
        rbook.position = ordering[unicode(rbook.book_id)]

    db.session.commit()


def add_to_reading_list(list_id, book_id):
    r = ReadingListBookAssociation()
    r.book_id = book_id
    r.reading_list_id = list_id
    r.position = 999

    db.session.add(r)
    db.session.commit()


def remove_from_reading_list(list_id, book_id):
    r = ReadingListBookAssociation.query.filter_by(reading_list_id=list_id, book_id=book_id).first()
    db.session.delete(r)
    db.session.commit()


def generate_genre_tree_select_options(selected=None, value_id=False):
    output = ""

    for parent in get_taxonomy_terms_without_parent('genre'):
        output += _recurse_select_level(parent, selected=selected, value_id=value_id)

    return output


def _recurse_select_level(parent, depth=0, selected=None, value_id=False):
    name = ("&mdash;" * depth) + " " + parent.name
    value = parent.id if value_id else parent.name

    selected_string = """ selected="selected" """ if selected == parent.name else ""

    output = """<option value="%s"%s>%s</option>""" % (value, selected_string, name)

    if parent.children:
        for child in parent.children:
            output += _recurse_select_level(child, depth=depth + 1, selected=selected, value_id=value_id)

    return output


def generate_genre_tree_list():
    output = ""

    for parent in get_taxonomy_terms_without_parent('genre'):
        output += _recurse_list_level(parent)

    return output


def _recurse_list_level(parent):
    output = "<li>"
    output += """<a href="#" data-tax-id="{}" data-tax-slug="{}"
                             data-tax-name="{}" data-tax-type="{}"
                             data-tax-parent="{}">{}</a>""".format(
        parent.id, parent.slug, parent.name, parent.type, parent.parent_id, parent.name)

    output += """ <span class="tax-count">{}</span>""".format(len(parent.books))

    if parent.children:
        output = output + "<ul>"
        for child in sorted(parent.children, key=lambda x: x.name):
            output = output + _recurse_list_level(child)

        output = output + "</ul>"

    output = output + "</li>"
    return output


def authenticate(username, password):
    hashed_pass = hashlib.sha1(password).hexdigest()
    user = db.session.query(User).filter(User.username==username).first()
    if user and user.password == hashed_pass:
        return user

    return None

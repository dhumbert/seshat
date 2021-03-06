import os
import os.path
import hashlib
import shutil
from flask import url_for
from sqlalchemy import event
from sqlalchemy.sql import expression
from sqlalchemy.ext.orderinglist import ordering_list
from sqlalchemy.ext.associationproxy import association_proxy
from literable import db, book_upload_set, book_staging_upload_set, cover_upload_set, tmp_cover_upload_set, utils, epub, app, pdf


books_taxonomies = db.Table('books_taxonomies',
    db.Column('taxonomy_id', db.Integer, db.ForeignKey('taxonomies.id')),
    db.Column('book_id', db.Integer, db.ForeignKey('books.id'))
)


class Taxonomy(db.Model):
    __tablename__ = 'taxonomies'
    id = db.Column(db.Integer, primary_key=True)
    parent_id = db.Column(db.Integer, db.ForeignKey('taxonomies.id'))
    parent = db.relationship('Taxonomy', backref=db.backref('children'), remote_side=[id])
    name = db.Column(db.String)
    name_sort = db.Column(db.String)
    type = db.Column(db.String)
    slug = db.Column(db.String)

    def get_parents(self):
        parents = []
        if self.parent_id:
            parent = Taxonomy.query.get(self.parent_id)
            if parent:
                parents.append(parent)
                parents = parent.get_parents() + parents

        return parents

    def generate_slug(self, depth=0):
        search_for = utils.slugify(self.name)

        if depth > 0:
            search_for = utils.slugify("%s-%d" % (self.name, depth))

        result = Taxonomy.query.filter_by(slug=search_for).first()
        if result is None:
            return search_for
        else:
            return self.generate_slug(depth + 1)

    @classmethod
    def get_grouped_counts(cls, ttype, order):
        q = db.session.query(Taxonomy.name_sort.label('name'), Taxonomy.slug, Taxonomy.id, Taxonomy.type,
                             Taxonomy.parent_id,
                         db.func.count(books_taxonomies.c.book_id).label('count_books'))\
        .filter_by(type=ttype)\
        .outerjoin(books_taxonomies).group_by(Taxonomy.name_sort, Taxonomy.slug, Taxonomy.id, Taxonomy.type, Taxonomy.parent_id)

        if not order or order == 'name':
            q = q.order_by(Taxonomy.name_sort.asc())
        elif order == 'count':
            q = q.order_by(db.desc('count_books'))

        return q.all()

    @classmethod
    def get_types(cls):
        """
        Returns a dict of type: hierarchical, e.g. genre: True, tag: False
        """
        types = {}
        results = db.session.query(db.distinct(Taxonomy.type), expression.case([(Taxonomy.parent_id == None, 0)], else_=1)).order_by(Taxonomy.type.asc()).all()
        for row in results:
            name = row[0]
            hierarchical = bool(row[1])

            if name not in types:
                types[name] = hierarchical
            else:
                if hierarchical and not types[name]:
                    types[name] = hierarchical

        return types


    def __repr__(self):
        return self.name


class Book(db.Model):
    __tablename__ = 'books'

    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String)
    title_sort = db.Column(db.String)
    filename = db.Column(db.String)
    cover = db.Column(db.String)
    description = db.Column(db.Text)
    created_at = db.Column(db.DateTime())
    public = db.Column(db.Boolean())
    series_seq = db.Column(db.Integer)
    batch = db.Column(db.String)
    id_isbn = db.Column(db.String)
    id_amazon = db.Column(db.String)
    id_google = db.Column(db.String)
    id_calibre = db.Column(db.String)
    word_count = db.Column(db.Integer)
    owned = db.Column(db.Boolean())

    taxonomies = db.relationship('Taxonomy', secondary=books_taxonomies, backref=db.backref('books'))

    user_id = db.Column(db.Integer, db.ForeignKey('users.id'))
    user = db.relationship('User', backref=db.backref('books'))

    @property
    def authors(self):
        return self._get_tax_by_type('author')

    @property
    def publishers(self):
        return self._get_tax_by_type('publisher')

    @property
    def tags(self):
        return self._get_tax_by_type('tag')

    @property
    def series(self):
        return self._get_tax_by_type('series')

    def _get_tax_by_type(self, type):
        return filter(lambda t: t.type == type, self.taxonomies)

    def estimated_reading_time(self, user):
        if self.word_count:
            return utils.format_duration(self.word_count / user.reading_speed)
        else:
            return None

    def get_cover_url(self):
        if self.cover:
            return cover_upload_set.url(self.cover)
        else:
            return url_for('static', filename='img/default.jpg')

    def get_format(self):
        if self.filename:
            return os.path.splitext(self.filename)[1][1:]
        else:
            return None

    def remove_file(self):
        if self.filename:
            try:
                os.remove(book_upload_set.path(self.filename))
                self.filename = None
            except Exception as e:
                app.logger.error("Unable to delete file: {}".format(e.message))
                pass  # oh, well

    def remove_cover(self):
        if self.cover:
            try:
                os.remove(cover_upload_set.path(self.cover))
                self.cover = None
            except Exception as e:
                app.logger.error("Unable to delete cover: {}".format(e.message))
                pass  # oh, well

    def move_cover_from_tmp(self, filename):
        if self.cover:
            self.remove_cover()

        if filename:
            self.cover = filename
            src = tmp_cover_upload_set.path(self.cover)
            dest_path = cover_upload_set.config.destination

            if os.path.exists(os.path.join(dest_path, self.cover)):
                self.cover = cover_upload_set.resolve_conflict(dest_path, self.cover)

            shutil.move(src, cover_upload_set.path(self.cover))

    def move_file_from_staging(self, filename):
        if self.filename:
            self.remove_file()

        if filename:
            self.filename = filename
            src = book_staging_upload_set.path(self.filename)
            dest_path = app.config['LIBRARY_PATH']

            if os.path.exists(os.path.join(dest_path, self.filename)):
                self.filename = book_upload_set.resolve_conflict(dest_path, self.filename)

            shutil.move(src, book_upload_set.path(self.filename))


    def attempt_to_update_cover(self, file):
        try:
            cover = cover_upload_set.save(file)
            if cover:
                if self.cover:
                    # remove current cover if user uploaded new one
                    try:
                        self.remove_cover()
                    except:
                        pass  # can't delete old cover. not the end of the world.

                self.cover = cover
        except:
            pass  # couldn't upload cover. maybe blank upload?

    def get_tag_string(self):
        tags = [tag.name for tag in self.tags]
        return ', '.join(tags)

    def update_taxonomies(self, tax_map):
        self.taxonomies = []
        for tax_slug, terms in tax_map.iteritems():
            for term_name in terms:
                if not term_name:
                    continue

                if isinstance(term_name, int):
                    tax = Taxonomy.query.get(term_name)
                    self.taxonomies.append(tax)
                else:
                    if isinstance(term_name, tuple):
                        n = term_name[0].strip()
                        name_sort = term_name[1].strip()
                    else:
                        n = term_name.strip()
                        name_sort = n

                    if n:
                        tax = Taxonomy.query.filter_by(type=tax_slug, name=n).first()
                        if tax:
                            tax.name_sort = name_sort
                            db.session.commit()
                        else:
                            tax = Taxonomy()
                            tax.type = tax_slug
                            tax.name = n
                            tax.name_sort = name_sort
                            tax.slug = tax.generate_slug()

                            db.session.add(tax)
                            db.session.commit()

                        if tax not in self.taxonomies:
                            self.taxonomies.append(tax)

    def write_meta(self):
        if self.filename:
            if self.get_format() == 'epub':
                self._write_epub_meta()

    def _write_epub_meta(self):
        epub_file = book_upload_set.path(self.filename)

        title = self._build_meta_title()

        if self.cover:
            cover = cover_upload_set.path(self.cover)
        else:
            cover = None

        if self.tags:
            tags = [tag.name for tag in self.tags]
        else:
            tags = []

        if self.authors:
            author = self.authors[0].name
        else:
            author = None

        subjects = tags

        epub.write_epub_meta(epub_file, title, author,
            description=self.description,
            cover=cover,
            subjects=subjects)

    def update_word_count(self, is_web_request=True):
        if self.filename:
            word_count = None
            if self.get_format() == 'epub':
                word_count = self._update_epub_word_count()
            # updating PDF word counts can get hairy, don't do that in web request
            # (require running manage.py command)
            elif self.get_format() == 'pdf' and not is_web_request:
                word_count = self._update_pdf_word_count()

            self.word_count = word_count
            db.session.commit()

    def _update_epub_word_count(self):
        epub_file = book_upload_set.path(self.filename)
        e = epub.Epub(epub_file)
        return e.count_words()

    def _update_pdf_word_count(self):
        pdf_file = book_upload_set.path(self.filename)
        p = pdf.PDF(pdf_file)
        return p.count_words()


    def _build_meta_title(self):
        title = self.title
        if app.config['ADD_SERIES_TO_META_TITLE']:
            if self.series:
                prepend_title = self.series[0].name
                if self.series_seq:
                    prepend_title = "%s %d - " % (prepend_title, self.series_seq)

                title = prepend_title + title
        return title

    def rate(self, score):
        self.rating = score
        db.session.commit()

    def __str__(self):
        return "[" + self.title + "]"

    def __repr__(self):
        return self.__str__()


class ReadingListBookAssociation(db.Model):
    __tablename__ = 'reading_list_books'
    reading_list_id = db.Column(db.Integer, db.ForeignKey('reading_lists.id', onupdate='cascade', ondelete='cascade'), primary_key=True)
    book_id = db.Column(db.Integer, db.ForeignKey('books.id', onupdate='cascade', ondelete='cascade'), primary_key=True)
    position = db.Column(db.Integer)

    book = db.relationship('Book', passive_deletes=True)


class ReadingList(db.Model):
    __tablename__ = 'reading_lists'
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'))
    name = db.Column(db.String)
    slug = db.Column(db.String)
    _books = db.relationship('ReadingListBookAssociation', order_by=[ReadingListBookAssociation.position],
                                    collection_class=ordering_list('position'), passive_deletes=True)
    books = association_proxy('_books', 'book')

    def generate_slug(self, depth=0):
        search_for = utils.slugify(self.name)

        if depth > 0:
            search_for = utils.slugify("%s-%d" % (self.name, depth))

        result = ReadingList.query.filter_by(slug=search_for).first()
        if result is None:
            return search_for
        else:
            return self.generate_slug(depth + 1)

    def __str__(self):
        return '{}'.format(self.name)

    def __repr__(self):
        return self.__str__()


class UserBookMeta(db.Model):
    __tablename__ = 'user_book_meta'
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), primary_key=True)
    book_id = db.Column(db.Integer, db.ForeignKey('books.id'), primary_key=True)
    hidden = db.Column(db.Boolean)
    rating = db.Column(db.Integer)
    notes = db.Column(db.Text)

    book = db.relationship(Book)


class User(db.Model):
    __tablename__ = 'users'
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String)
    password = db.Column(db.String)
    admin = db.Column(db.Boolean)
    reading_wpm = db.Column(db.Integer)

    reading_lists = db.relationship(ReadingList, order_by=[ReadingList.name], backref=db.backref('user'))
    book_meta = db.relationship(UserBookMeta, backref=db.backref('user'))

    # to keep track of book meta objects before they're committed to DB
    staging_book_meta = {}

    @property
    def reading_speed(self):
        return self.reading_wpm if self.reading_wpm else 250

    def get_reading_list(self, slug):
        for rlist in self.reading_lists:
            if rlist.slug == slug:
                return rlist

    def get_reading_list_by_id(self, id):
        for rlist in self.reading_lists:
            if rlist.id == id:
                return rlist

    def book_is_in_reading_list(self, reading_list_id, book_id):
        list = self.get_reading_list_by_id(reading_list_id)
        return book_id in [book.id for book in list.books]

    def get_book_meta(self, book_id):
        for b in self.book_meta:
            if b.book_id == int(book_id):
                return b

        # if we didn't find it, create a new one
        m = UserBookMeta()
        m.user_id = self.id
        m.book_id = book_id
        db.session.add(m)
        #db.session.commit()
        return m

    @property
    def rated_books(self):
        return [b.book for b in sorted(self.book_meta, key=lambda x: x.rating, reverse=True) if b.rating]

    def has_hidden_book(self, book_id):
        return self.get_book_meta(book_id).hidden

    def can_modify_book(self, book):
        return self.admin or book.user_id == self.id

    @property
    def has_unread(self):
        return any([x for x in self.received_recommendations if not x.seen])

    @property
    def unread_count(self):
        return len([x for x in self.received_recommendations if not x.seen])

    def mark_recommendations_seen(self):
        for r in self.received_recommendations:
            r.seen = True

        db.session.commit()

    def remove_recommendation(self, from_user_id, book_id):
        for r in self.received_recommendations:
            if r.from_user_id == from_user_id and r.book_id == book_id:
                db.session.delete(r)
                db.session.commit()
                break


    def is_authenticated(self):
        return True

    def is_active(self):
        return True

    def is_anonymous(self):
        return False

    def get_id(self):
        return unicode(self.id)

    def set_password(self, password):
        self.password = hashlib.sha1(password).hexdigest()

    def __repr__(self):
        return unicode(self.username)


class Recommendation(db.Model):
    __tablename__ = 'recommendations'
    from_user_id = db.Column(db.Integer, db.ForeignKey('users.id'), primary_key=True)
    to_user_id = db.Column(db.Integer, db.ForeignKey('users.id'), primary_key=True)
    book_id = db.Column(db.Integer, db.ForeignKey('books.id'), primary_key=True)
    seen = db.Column(db.Boolean)
    message = db.Column(db.String)
    created_at = db.Column(db.DateTime)

    book = db.relationship(Book)
    from_user = db.relationship(User, foreign_keys=[from_user_id], backref=db.backref('sent_recommendations'))
    to_user = db.relationship(User, foreign_keys=[to_user_id], backref=db.backref('received_recommendations'))
